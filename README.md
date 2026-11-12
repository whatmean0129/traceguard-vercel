# TraceGuard · 图片隐形水印与溯源演示（Vercel 部署版）

基于 8×8 分块 DCT 关系编码的**盲水印**技术验证项目（POC）。本仓库是面向 Vercel
Serverless 的最小部署单元，只包含运行所需的文件；不含实验数据、历史报告、真实照片、
个人路径或密钥。

> 本项目用于教学与技术演示，不是生产级版权保护、身份认证或取证系统。

---

## 1. 项目简介与应用场景

把一串 16 bit 编号隐形嵌入图片，之后在**不需要原图**的情况下把编号读回来，用于演示
「图片被转发、被 JPEG 重新压缩后，仍能追溯它最初分发给谁」。

典型演示场景：

- 给不同接收方分发带不同编号的同一张图片，出现外泄时根据提取出的编号追溯来源；
- 观察 JPEG 压缩强度对水印存活的影响，以及嵌入水印对画质的影响；
- 展示盲水印、多数投票、校验和等概念在真实代码中的实现。

页面提供上传图片 → 嵌入编号 → 提取编号 → JPEG 压缩攻击 → 查看指标的完整交互。
所有数值来自当次请求的实际计算，不使用预置或历史数据。

## 2. DCT 水印基本原理

1. 取图片亮度通道 Y，切成互不重叠的 8×8 块，每块做 DCT，得到 64 个频率系数。
2. 低频改动肉眼明显、高频在 JPEG 量化时容易被抹成 0，因此只改**中频**。
   这里选 `(2,1)` 与 `(1,2)`：在 JPEG 标准亮度量化表中两者的量化步长接近，
   压缩后一起缩水，大小关系不易翻转。
3. **关系编码**：用两个系数的大小关系表示 1 bit。`bit=1` 时令
   `C_A − C_B ≥ +strength`，`bit=0` 时令 `C_A − C_B ≤ −strength`。
   `strength` 是人为留出的裕量，`margin` 用于抵消像素取整带来的损失。
4. **盲提取**：不需要原图，对同样位置做 DCT，看 `C_A − C_B` 的正负即可读出 bit。
5. **冗余与多数投票**：32 bit 载荷（8 bit 同步头 + 16 bit 编号 + 8 bit CRC-8）
   循环写满所有块，每个 bit 得到多张票，提取时按多数投票判定。
6. **校验**：同步头与 CRC-8 均通过校验后，认为提取结果通过格式与校验检查。
   未嵌入水印的图片会被判为无效，不会随意返回一个编号。

工程细节：图片以 BGR 保存，提取端读到的是颜色空间往返换算后的 Y，取整会带来 ±1
误差，因此嵌入时在「最终能被观测到的 Y」上做闭环修正。固定伪随机种子决定水印散布到
哪些块，嵌入与提取必须一致。

默认参数：`strength=16`、`margin=8`、编号范围 `0–65535`。

## 3. 当前 POC 已实现的功能

- **网页界面**（`/app`、`/app/`）：上传图片、嵌入编号、提取编号、JPEG 压缩攻击、
  结果对比与指标展示，静态资源位于 `/static/`。
- **两套接口协议**：
  - `/api/analyze`、`/api/embed`、`/api/extract`、`/api/attack`
    接受 JSON + Base64 DataURL，返回 `{ok:true,data:…}` 或
    `{ok:false,error,hint}`（恒 200，带中文提示）；
  - `/api/v1/embed`、`/api/v1/extract` 接受 `multipart/form-data`，
    `/api/v1/health`、`/api/v1/info`、`/api/v1/output/<name>` 提供状态、配置与结果图。
  - `GET /` 返回服务信息 JSON（不是页面）；页面入口是 `/app`。
- **真实运算**：嵌入、盲提取、JPEG 攻击、图片预检、核心算法文件 SHA-256 校验
  都是实际调用，不返回估算值或伪造结果。
- **指标展示**：PSNR、SSIM、块数、未收敛块数、同步头/CRC 判定、投票一致率、
  误码统计、图片元信息。
- **部署适配**：响应体超过阈值时主动返回 HTTP 413 与可解析的中文 JSON 提示；
  其余 HTTP 状态码原样透传。
- **异常图检测**：无法解码、格式不符、缺字段等输入返回结构化错误，不伪造成功。

后端仅使用 Python 标准库 `http.server`，不依赖 Web 框架。

## 4. 本地运行方法

**依赖**：Python 3.12（见 `.python-version`），以及：

```
numpy==2.4.6
opencv-python-headless==5.0.0.93
scipy==1.17.1
```

安装：`pip install -r requirements.txt`

**启动**：本仓库是 Vercel Serverless 形态，`api/index.py` 顶层导出
`handler`（`http.server.BaseHTTPRequestHandler` 子类）。本地运行可用标准库挂一个
临时 HTTP 服务，例如在项目根目录执行：

```python
from http.server import ThreadingHTTPServer
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("tg_index", os.path.join("api", "index.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["tg_index"] = mod
spec.loader.exec_module(mod)
ThreadingHTTPServer(("127.0.0.1", 8000), mod.handler).serve_forever()
```

然后访问 `http://127.0.0.1:8000/app`。

> **本地与线上的差别**：适配层把运行时写盘目录固定为 `/tmp/traceguard_vercel/`
> 下的独立子目录，这是为 Vercel 只读文件系统设计的。在 Windows 等非 Linux 环境
> 本地运行时，需要在进程内把 `config.OUTPUT_DIR / LOG_DIR / UPLOAD_DIR /
> EVIDENCE_DIR` 与 `compat._LOG_DIR` 重映射到本机可写的临时目录，否则涉及落盘的
> 接口会报错。本项目已在本地以真实 HTTP 请求完成过适配层冒烟测试（页面、静态资源、
> 健康检查、错误处理、嵌入与提取、413 预检、状态码透传）；该结果**只说明本地接口
> 可用**，不代表公网部署通过。

## 5. Vercel 部署说明

- 将本仓库导入 Vercel，**Root Directory 保持仓库根目录**（本仓库根即部署根，
  不需要指向子目录）。
- `.python-version` 指定 Python 3.12；构建期按 `requirements.txt` 安装依赖。
- `api/index.py` 位于官方 `/api` 目录，顶层导出 `handler`（`BaseHTTPRequestHandler`
  子类），是该形态支持的入口之一。
- `vercel.json` 用两条 rewrite 把页面与接口请求统一转发到 `/api`，并通过查询参数
  还原原始路径；`source` 用 negative lookahead 排除精确的 `/api`，避免 rewrite 循环。
- `.vercelignore` 在上传/部署层排除文档、实验数据、真实图片、日志、密钥类文件与
  虚拟环境；`functions` 打包采用 Python Runtime 默认行为。
- Hobby 计划：`maxDuration` 默认且上限 300 s，函数包上限 500 MB（未压缩），
  请求体与响应体上限 4.5 MB。
- 运行时文件系统只读，仅 `/tmp` 可写（上限 500 MB）。

> **本仓库尚未进行公网部署**。rewrite 目标是否命中函数、默认打包是否保留
> `api/` 与 `webapp/` 的相对层级、`/tmp` 子目录能否创建、Linux 下依赖构建结果、
> 应用层 413 是否被平台原样透传、公网端到端交互，均需部署后实测。

## 6. 演示环境与正式实验结论的区别

- 本仓库**不包含任何实验数据、结果表格、历史报告或真实照片**；`README` 与页面
  中的说明不构成实验结果。
- 项目另有独立保存的阶段性实验数据与结论，未包含在本部署仓库中，**也未被重跑**。
- 公网演示中产生的任何 PSNR / SSIM / 误码率 / 一致率等数值，一律属于
  **「演示环境数值」**，不得写成「与正式实验一致」或作为实验结论引用。
- 目标运行环境是 Python 3.12 + Linux；其与本地 Python 3.11 环境在 OpenCV DCT
  浮点结果上是否逐位一致**尚未验证**，因此不承诺跨平台数值一致。
- 上文提到的本地 HTTP 冒烟测试属于**适配层接口可用性验证**，不是水印算法性能、
  鲁棒性或正确性的结论，更不是公网部署验证。

## 7. 临时存储、数据持久性与请求大小限制

- 运行时写盘统一落在 `/tmp/traceguard_vercel/` 下四个独立子目录：
  `output`、`logs`、`uploads`、`evidence`，避免同名文件互相覆盖。
- 这些目录**只服务单次请求，不保证跨请求、跨实例持久化**。因此
  `/api/v1/output/<name>` 仅在单实例内有效，二次 GET 可能返回 404；
  前端兼容接口改为直接返回 Base64 结果图，不依赖该路径。
- 平台对**请求体和响应体**都有 4.5 MB 上限：
  - **响应体**：适配层在响应超过 4,400,000 字节时主动返回
    `HTTP 413` + `{ok:false,error,hint}` 的小体积 JSON，页面据此显示中文提示，
    避免无 JSON 的空白错误页。这是容量保护，**不是水印算法失败**。
  - **请求体**：超过 4.5 MB 的上传由平台层直接 413，函数收不到请求，
    应用层无法拦截，需在上传前控制图片大小。
- 因此公网演示对大图存在**功能收缩**（部分大图会被拒绝并提示换用较小的图），
  本地整合版不受此限制。

## 8. 当前技术限制与免责声明

- **只针对 JPEG 有损压缩设计**：高压缩强度下编号可能无法还原；低质量因子的
  失效边界依图片内容而异，本项目不给出通用保证。
- **不抗几何攻击**：裁剪、缩放、旋转会破坏 8×8 块对齐，当前版本未实现块同步，
  相关场景未验证。
- **不做来源认证**：CRC-8 只校验比特流自洽，不能证明图片由谁发出；
  文件 SHA-256 校验只用于识别代码版本，**不是防篡改机制**。
- **不做篡改检测**：系统只能回答「这张图带的是哪个编号」，不能判断图像内容
  是否被修改过。
- **无身份认证与访问控制**：接口公开，任何访问者都可调用；请勿上传含敏感
  信息的图片。
- **第三方托管**：部署后源码与用户上传的图片会交由云平台处理；Hobby 计划
  仅限个人/非商业用途，平台条款可能涉及内容用于模型训练，使用者需自行确认。
- 本项目是 **POC**，按「原样」提供，不提供任何形式的保证，不适用于生产环境、
  法律效力认定或真实版权纠纷举证。
- 尚未进行公网部署与端到端验证；未经实测的平台行为不在本文承诺范围内。

---

## 附：仓库文件

```
.gitattributes      统一 LF 行尾
.gitignore          排除实验数据、图片、日志、密钥、虚拟环境等
.python-version     Python 3.12
.vercelignore       Vercel 上传层排除规则
vercel.json         rewrites 配置
requirements.txt    Linux 兼容依赖（版本锁定）
traceguard_dct.py   DCT 水印核心算法
api/index.py        Vercel 函数入口（运行时适配层）
webapp/app/         后端服务、协议兼容层与图像处理
webapp/static/index.html  前端页面
```
