# Third-party components

This repository contains independent installation/lifecycle scripts, configuration templates, documentation and AI skill instructions. Its MIT license applies to those original files. Upstream code, binaries, trademarks and client software retain their respective terms; they are not relicensed by this project.

| Component | Pinned version | Upstream | License at pinned version |
|---|---|---|---|
| wechatauto-replica | 1.2.6.1 | [Repository](https://github.com/fanyuantaier/wechatauto-replica) | [Apache-2.0](https://github.com/fanyuantaier/wechatauto-replica/blob/v1.2.6.1/LICENSE) |
| QQ Chat Exporter / QCE Framework | 6.3.2 | [Release](https://github.com/shuakami/qq-chat-exporter/releases/tag/v6.3.2) | [GPL v3 text](https://github.com/shuakami/qq-chat-exporter/blob/v6.3.2/LICENSE) |
| NapCatQQ, bundled in the QCE Framework asset | 4.18.28 | [Repository](https://github.com/NapNeko/NapCatQQ) | [Limited Redistribution License for NapCat](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/LICENSE) |

The pinned NapCat license includes a non-commercial-use condition and restrictions on redistribution and public release of modified code. Do not describe the complete deployed stack as unrestricted MIT software or assume commercial-use rights. Review the linked upstream text before using or redistributing those components.

Upstream assets are downloaded from their original release locations during local installation and verified against `upstream-lock.json`. The public source/release candidate excludes the downloaded wheel, ZIP, extracted binaries, node_modules and Python environment. We do not publish modified upstream program code or bundle a QQ/WeChat client.

Runtime configuration sets local authentication, log levels and disabled management UI; upstream program implementations remain unchanged. Existing desktop WeChat and QQ are installed and licensed separately by their operator.

Python transitive dependencies are installed from package indexes according to `requirements.lock` and retain their own licenses. The lock fixes the tested Windows/Python version set; it is not a claim to license all upstream dependencies under MIT.
