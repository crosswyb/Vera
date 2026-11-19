# Python runtime notices

`LICENSE.txt` is copied verbatim from the Python 3.12 runtime used to build the bundled backend. `OpenSSL-LICENSE.txt` is the upstream license for its OpenSSL 3.5.8 component, obtained from the pinned upstream tag:

https://github.com/openssl/openssl/blob/openssl-3.5.8/LICENSE.txt

Python packages and build hooks have separate notices under `third_party/python/`. macOS system frameworks are supplied by the operating system, not copied into the distribution. Apple Speech language assets are downloaded by the system on the recipient's Mac and are not bundled.
