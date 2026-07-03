# Chat-ReCreate

Chat-ReCreate 是一个对话式广告复刻工作台：用户先从案例库选择源视频，再选择或上传要替换的商品图，系统会用 Gemini/Compass 理解视频、推断可改编意图、编译 Seedance Prompt，并提交真实 Seedance 生成任务。前端负责对话式选择、Prompt 编译状态和源视频/复刻视频对照；后端负责案例库、商品库、会话持久化、模型调用和生成产物管理。

本目录是独立运行项目，源项目仅作为设计与链路参考，没有软链接或运行时依赖。公开仓库只提交代码、示例配置和可公开素材；真实 Compass API 凭据必须由运行环境提供，不能提交到 Git。

## 启动

默认服务绑定 `0.0.0.0:5188`，方便同一局域网内的设备访问。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

本机打开 `http://127.0.0.1:5188`。局域网设备访问时，把 `127.0.0.1` 换成这台机器的 IP，例如 `http://<your-ip>:5188`。

如需改端口或只绑定本机：

```bash
PORT=5190 python server.py
HOST=127.0.0.1 python server.py
```

## 模型配置

视频理解、创意建议、意图改写和 Seedance Prompt 编译默认全部调用 Gemini/Compass；生成按钮直接提交真实 Seedance 任务，不存在本地模拟或静默降级路径。启动前必须设置：

```bash
cp .env.example .env
export COMPASS_API_KEY="..."
export COMPASS_BASE_URL="..."
export CHAT_RECREATE_MODEL="gemini-3.5-flash"              # 可选
export SEEDANCE_MODEL="dreamina-seedance-2-0-260128"       # 可选
export SEEDANCE_REQUEST_TIMEOUT_SEC="300"                   # 可选
```

商品库中带公网图片地址的商品可直接用于 Seedance。用户本地上传的图片可以参与 Gemini Prompt 编译，但在提交 Seedance 前仍需配置公网可访问的图片地址；接口会明确返回错误，不会退回文本模拟。

`.env`、`.env.*`、运行日志、会话状态和生成产物都被 `.gitignore` 排除。开源版本不会从本机私有项目或私有配置模块自动读取凭据；缺少 `COMPASS_API_KEY` 或 `COMPASS_BASE_URL` 时，真实模型接口会返回明确错误。

## GitHub Pages 展示

仓库包含 `.github/workflows/pages.yml`。推送到 `main` 或 `codex/open-source` 后，GitHub Actions 会把 `chat-recreate/` 作为静态站点发布到 GitHub Pages。

Pages 版本用于公开展示 UI、案例库和 Prompt 预览流程，不连接 Compass，也不会提交 Seedance 生成任务。真实 API、会话保存、历史案例和视频生成需要按上面的方式本地运行 FastAPI 后端。

## 数据与产物

- `cases.json`：包含 20 条源视频的独立创意库
- `experiments/outputs/<case>/variant_H_hero_only/response.json`：Gemini 视频理解缓存
- `runtime/sessions/<session_id>.json`：服务端会话状态与完整用户/Agent 对话
- `experiments/runs/<case>/<run_id>/`：对话、intent、prompt 与生成产物
- `runtime/latest/<case>.json`：刷新恢复指针

前端不使用 `localStorage` 保存工作流。选择灵感案例时会立即创建服务端会话，
后续商品、意图、消息和生成状态都采用原子文件替换写入。每个会话带 revision，
旧 revision 的并发写入会返回 409，避免多个页面互相覆盖。

“案例库”可以读取新会话和已有 run。点击案例会打开完整任务流程；未完成会话可
继续编辑，已完成案例可在正常工作台里查看对话和最终视频。刷新编辑页时，通过
URL 中的 `?session=<session_id>` 从服务端恢复。

`GET /api/health` 只报告配置是否齐全，不返回密钥。缺少 Compass 配置时，真实模型接口会返回可重试的明确错误。

## 批量运行

```bash
python experiments/run_all_cases.py
```

批次会持久化到 `experiments/batches/<batch_id>/`，每个案例的模型输入、Prompt、
任务记录和 MP4 保存在独立的 `experiments/runs/<case>/<run_id>/`。服务运行时打开
`http://127.0.0.1:5188/results` 可查看最新批次的源视频/成片对照页。
