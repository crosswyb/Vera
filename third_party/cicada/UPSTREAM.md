# Cicada extraction record

- Upstream: https://github.com/wquguru/cicada
- Fixed revision: `31574edbd43486eb5c4b958f82b503a63bb4697d` (release v1.10.1, 2026-11-14).
- Inspected and extracted: 2026-11-15.
- Upstream license: Apache-2.0; verbatim license retained in `LICENSE`.
- This folder contains provenance and license, not a complete second copy of Cicada.

| Upstream source | SHA-256 of inspected original | Adapted destination |
|---|---|---|
| `Sources/Cicada/SystemAudioTap.swift` | `25253e0df4b41dfb9cba28050c30cd0b84c32b1699b671e2501234d26ceeed3c` | `native/Sources/AudioCore/SystemAudioTap.swift` |
| `Sources/Cicada/Recognition.swift` | `26a125622314b3a5cefeeefe22f587857c807277509ab0fd101f7c8b4041eace` | Modern Analyzer only, `native/Sources/AudioCore/LocalRecognition.swift` |
| `Sources/Cicada/AudioFiles.swift` | `b086ade8fa2825ff0ce35e34ec7050a32df08d3cbea49dcea95e6285387d18b9` | PCMConverter only, `native/Sources/AudioCore/PCMConverter.swift` |
| `LICENSE` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` | `third_party/cicada/LICENSE` |

Changed files carry an adaptation notice. The capture implementation retains the upstream process-tap/aggregate/HAL topology, output-headset channel mapping and distinction between nominal and actual sample rates. It uses a new bundle/device identity, checked startup errors and explicit device-change gaps. Private TCC preflight and localization/logging dependencies were removed. Permission is never inferred from silence.

The local recognizer retains the upstream SpeechAnalyzer/SpeechTranscriber and fast volatile-result pattern. The large Recognition facade, legacy OS/Intel/cloud engines, language detection, translation, original AppModel/settings/storage, original app UI, cloud credentials, voiceprints, virtual microphone, audio-file recording and speech output are excluded. Original AudioRouter/ListenEngine are **not** embedded: a smaller bounded stream consumer and revision reducer handle the chosen single-locale path. `MicrophoneSource` is an independent minimal AVAudioEngine adapter to the same capture contract.

No dependency is made on `/Applications/Cicada.app`; that installation is not modified. Apple Speech model assets are provided by the operating system, are not bundled or relicensed here, and have no cloud-recognition fallback in this component.

Future upstream updates require reviewing these exact three source regions and rerunning capture, sample-rate, lifecycle and transcript-revision validation. Updating a source pin alone does not update the extracted code.
