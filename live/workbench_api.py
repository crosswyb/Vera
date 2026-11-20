"""Register v2 routes under the existing authentication/Origin boundary."""

import os
import threading
import time

from flask import g, jsonify, render_template, request

from live.model_budget import ModelBudget
from live.model_transport import ModelRequestError
from live.provider_settings import ProviderSettings, SettingsUpdate
from live.store import LiveError, require
from live.workbench import Workbench
from live.workbench_contracts import AudioEvent, AudioImport, AudioImportFinish, DeleteMeeting, EditQuestion, MeetingControl, MeetingPermissions, NewMeeting, NewQuestion, QuestionAction, RecordingStart, RenameMeeting, Utterance


def register_workbench(app, store):
    def enabled(key):
        return os.getenv(key, "false").lower() in {"1", "true", "yes"}

    settings = ProviderSettings(app.config.get("WORKBENCH_SETTINGS_PATH"))
    caps = {**settings.current.capabilities(), "settings_editable": settings.path is not None,
            "model_meeting_ids": list(filter(None, os.getenv("ZSJH_WB_MODEL_MEETING_IDS", "").split(","))),
            "search_meeting_ids": list(filter(None, os.getenv("ZSJH_WB_SEARCH_MEETING_IDS", "").split(","))),
            "audio_enabled": enabled("ZSJH_WB_AUDIO_APPROVED"), "retention_days": None,
            "model_request_limit": ModelBudget.parse_limit(os.getenv("ZSJH_WB_MODEL_REQUEST_LIMIT") or os.getenv("ZSJH_LIVE_MODEL_REQUEST_LIMIT", "6")),
            "search_request_limit": ModelBudget.parse_limit(os.getenv("ZSJH_WB_SEARCH_REQUEST_LIMIT", "10")),
            "research_slots": 2, "native_audio_status": "development_build", "tencent_voice_status": "not_verified"}
    caps.update(app.config.get("WORKBENCH_CAPABILITIES", {}))
    root = app.config.get("WORKBENCH_ROOT") or os.getenv("ZSJH_WB_OUTPUT_ROOT") or store.path.parent / "workbench-meetings"
    wb = Workbench(store, root, caps)
    wb.provider_settings = settings
    from live.feishu_api import register_feishu
    register_feishu(app, wb)
    app.extensions["workbench"] = wb
    app.extensions["workbench_settings"] = settings
    probe_slot = threading.BoundedSemaphore(1)

    def settings_owner():
        require(g.member["role"] == "owner", "只有工作区主持人可以配置服务", 403)

    @app.get("/live/v2/storage")
    def workbench_storage():
        return jsonify(wb.storage(g.member))

    @app.put("/live/v2/storage")
    def workbench_save_storage():
        return jsonify(wb.set_storage(g.member, request.get_json(silent=True)))

    @app.get("/live/v2/settings")
    def workbench_settings():
        settings_owner()
        return jsonify(settings.public())

    @app.put("/live/v2/settings")
    def workbench_save_settings():
        settings_owner()
        update = body(SettingsUpdate)
        # Prepare before committing, then swap the pair in one scheduler lock.
        # Audio ingestion and the running task's immutable clients are untouched.
        with settings.lock:
            candidate = settings.preview(update)
            runtime = getattr(wb, "runtime", None)
            services = runtime.prepare_configuration(candidate) if runtime else None
            settings.commit(candidate)
            if runtime:
                runtime.install_configuration(services, candidate.capabilities())
            else:
                caps.update(candidate.capabilities())
            return jsonify(**settings.public(), capabilities=dict(caps))

    @app.post("/live/v2/settings/test/<kind>")
    def workbench_test_settings(kind):
        settings_owner()
        require(kind in {"model", "search"}, "未知服务", 404)
        candidate = settings.preview(body(SettingsUpdate))
        require(probe_slot.acquire(blocking=False), "连接测试正在进行，请稍候。", 409)
        from live.workbench_runtime import OutcomeUnknown, TextModel
        from live.source_reader import DeepSeekSearch, SearchOutcomeUnknown, TavilySearch, read_sources
        started = time.monotonic()
        try:
            if kind == "model":
                require(candidate.model.configured, "请先填写 BaseURL、模型名称和 APIKey；本机免密服务可留空密钥。", 400)
                model = TextModel(ModelBudget(limit=os.getenv("ZSJH_WB_MODEL_REQUEST_LIMIT")), candidate.model)
                result = model.call('This is a synthetic connection test. Return only JSON: {"ok":true}.', {"test": "synthetic"})
                require(isinstance(result, dict) and result.get("ok") is True, "模型已响应，但未返回预期 JSON；请检查模型与输出参数。", 400)
                return jsonify(ok=True, message="模型已连接，JSON 回答正常。", elapsed_ms=round((time.monotonic() - started) * 1000))
            key = candidate.search.key(candidate.model)
            require(bool(key), "请填写搜索服务的 APIKey。", 400)
            provider = TavilySearch if candidate.search.provider == "tavily" else DeepSeekSearch
            search = provider(key, ModelBudget(path=os.getenv("ZSJH_WB_SEARCH_USAGE_DB", ".zsjh/workbench-search-usage.sqlite3"),
                limit=os.getenv("ZSJH_WB_SEARCH_REQUEST_LIMIT", "10")), candidate.search.base_url)
            results = search.search("Python official documentation")
            sources = read_sources(results)
            return jsonify(ok=True, message=f"搜索已连接，返回 {len(results)} 条结果、{len(sources)} 份可引用正文。",
                           source_count=len(sources), elapsed_ms=round((time.monotonic() - started) * 1000))
        except LiveError:
            raise
        except Exception as exc:
            from live.failure_diagnostics import describe_failure
            from live.output_validation import failure_message
            details = describe_failure(exc, "detecting" if kind == "model" else "searching", "model" if kind == "model" else "search")
            status = getattr(exc, "status", None)
            if status is None and str(exc).startswith("search HTTP "):
                try:
                    status = int(str(exc).rsplit(" ", 1)[1])
                except ValueError:
                    pass
            if isinstance(exc, ModelRequestError):
                message = exc.public_message()
            elif status:
                message = f"服务返回 HTTP {status}，请检查地址、密钥和模型权限。"
            else:
                message = ("请求结果未知，请检查网络后手动重试。" if isinstance(exc, (OutcomeUnknown, SearchOutcomeUnknown)) else
                    "调用次数已达当前配置上限。" if isinstance(exc, RuntimeError) and "limit reached" in str(exc) else
                    "服务未返回预期格式，请检查接口类型与高级参数。")
            if isinstance(exc, (OutcomeUnknown, SearchOutcomeUnknown)):
                message = failure_message(details) + " " + details.get("suggestion", "")
            return jsonify(ok=False, message=message, error_details=details, elapsed_ms=round((time.monotonic() - started) * 1000))
        finally:
            probe_slot.release()

    def body(contract):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise LiveError("请求必须是 JSON 对象")
        return contract.model_validate(data)

    @app.get("/workbench")
    def workbench_page():
        return render_template("workbench.html")

    @app.get("/live/v2/home")
    def workbench_home():
        return jsonify(wb.home(g.member))

    @app.post("/live/v2/meetings")
    def workbench_create():
        return jsonify(wb.create(g.member, body(NewMeeting))), 201

    @app.patch("/live/v2/meetings/<mid>")
    def workbench_rename(mid):
        return jsonify(wb.rename_meeting(g.member, mid, body(RenameMeeting).title))

    @app.get("/live/v2/meetings/<mid>/deletion")
    def workbench_deletion_preview(mid):
        return jsonify(wb.deletion_preview(g.member, mid))

    @app.delete("/live/v2/meetings/<mid>")
    def workbench_delete(mid):
        return jsonify(wb.delete_meeting(g.member, mid, body(DeleteMeeting).confirmation))

    @app.get("/live/v2/meetings/<mid>/snapshot")
    def workbench_snapshot(mid):
        return jsonify(wb.snapshot(g.member, mid))

    @app.get("/live/v2/meetings/<mid>/segments")
    def workbench_earlier_segments(mid):
        before = request.args.get('before', type=int)
        require(before is not None and 0 < before <= 2**63 - 1, '发言分页位置无效', 400)
        return jsonify(wb.earlier_segments(g.member, mid, before))

    @app.post("/live/v2/meetings/<mid>/utterances")
    def workbench_utterance(mid):
        return jsonify(wb.utterance(g.member, mid, body(Utterance)))

    @app.post("/internal/live/v2/meetings/<mid>/audio-events")
    def workbench_audio(mid):
        require(caps.get("audio_enabled"), "本机采音接入尚未启用", 409)
        return jsonify(wb.audio(g.member, mid, body(AudioEvent)))

    @app.post("/internal/live/v2/meetings/<mid>/audio-import")
    def workbench_import(mid):
        require(caps.get("audio_enabled"), "本机音频接入尚未启用", 409)
        return jsonify(wb.begin_import(g.member, mid, body(AudioImport)))

    @app.post("/internal/live/v2/meetings/<mid>/recordings")
    def workbench_recording(mid):
        require(caps.get("audio_enabled"), "本机音频接入尚未启用", 409)
        return jsonify(wb.start_recording(g.member, mid, body(RecordingStart)))

    @app.post("/internal/live/v2/meetings/<mid>/audio-import/finish")
    def workbench_import_finish(mid):
        return jsonify(wb.finish_import(g.member, mid, body(AudioImportFinish)))

    @app.get("/live/v2/meetings/<mid>/transcript")
    def workbench_transcript(mid):
        return jsonify(wb.transcript(g.member, mid))

    @app.get("/live/v2/meetings/<mid>/files/<kind>")
    def workbench_meeting_file(mid, kind):
        return jsonify(wb.meeting_file(g.member, mid, kind))

    @app.post("/live/v2/meetings/<mid>/files/retry")
    def workbench_retry_archive(mid):
        require(request.get_json(silent=True) == {}, "请求不接收额外参数", 400)
        return jsonify(wb.retry_archive(g.member, mid))

    @app.post("/live/v2/meetings/<mid>/summary")
    def workbench_summary(mid):
        require(request.get_json(silent=True) == {}, "请求不接收额外参数", 400)
        return jsonify(wb.retry_summary(g.member, mid))

    @app.post("/live/v2/meetings/<mid>/permissions")
    def workbench_permissions(mid):
        return jsonify(wb.permissions(g.member, mid, body(MeetingPermissions)))

    @app.post("/live/v2/meetings/<mid>/questions")
    def workbench_question(mid):
        return jsonify(wb.question(g.member, mid, body(NewQuestion))), 201

    @app.patch("/live/v2/questions/<qid>")
    def workbench_edit(qid):
        return jsonify(wb.edit(g.member, qid, body(EditQuestion)))

    @app.post("/live/v2/questions/<qid>/actions")
    def workbench_action(qid):
        return jsonify(wb.action(g.member, qid, body(QuestionAction)))

    @app.post("/live/v2/meetings/<mid>/controls")
    def workbench_controls(mid):
        return jsonify(wb.control(g.member, mid, body(MeetingControl).action))

    @app.post("/live/v2/meetings/<mid>/demo")
    def workbench_demo(mid):
        require(request.get_json(silent=True) == {}, "演示不接收额外参数", 400)
        return jsonify(wb.demo(g.member, mid))

    @app.get("/live/v2/artifacts/<did>")
    def workbench_artifact(did):
        return jsonify(wb.artifact(g.member, did))

    return wb
