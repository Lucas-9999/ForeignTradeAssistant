# ForeignTradeAssistant CRM Console

React、TypeScript 与 Vite 前端，通过 FastAPI 的 `/ui/` 路径提供界面。TanStack Query 管理服务端状态，Recharts 提供图表。

## 演示

在项目根目录安装 Python 依赖，然后：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
python tools/run_portfolio_demo.py
```

打开 `http://127.0.0.1:8001/ui/`。所有演示记录为虚构内容，写入、发送、采集和提供商测试被拒绝。

## 前端开发

开发服务器的 API 代理指向本机 8000 端口，先从项目根目录启动合成演示：

```bash
python tools/run_portfolio_demo.py --port 8000
```

另一个终端：

```bash
cd frontend
npm ci
npm run dev
```

## 代码组织与验证

- `src/features/`：仪表盘、线索、标签、分组、邮箱和邮件中心。
- `src/components/ui/`：共享界面组件。
- `src/services/`、`src/hooks/`：API 客户端与 TanStack Query hooks。
- `src/lib/`：工具与中文映射。

```bash
npm run lint
npm run build
```

依赖复核见 [dependency-review.md](../docs/dependency-review.md)，项目架构与演示边界见根目录 README。
