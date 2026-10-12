# 展示版依赖复核（2026-10-12）

对象是 `frontend/package-lock.json` 的构建／开发依赖链；受影响包数量不能当成独立漏洞数量或密钥泄露证据。

## 已修补

`source-map-js` 从 1.2.1 更新至 1.2.2，仅此包变动，前端构建回归通过。

## 剩余问题

npm audit 剩余 **7 个受影响包：5 high、2 moderate、0 critical**，来自两项底层公告的依赖传播：

| 底层包 | 影响和依赖链 | 处理边界 |
| --- | --- | --- |
| `braces` | 栈耗尽／DoS；通过 chokidar、micromatch、fast-glob 进入 Tailwind 3 链 | 当前 3.0.3 分支本次核对无上游补丁，不运行 audit fix --force 强行迁移 |
| `postcss-selector-parser` | 恶意选择器造成二次复杂度 CPU 消耗，经 postcss-nested 进入构建链 | 修复为 7.1.6，当前链锁定 6.x，需配套验证迁移 |

这些是依赖检查结果，不代表演示已发生攻击或数据泄露。演示仅监听 loopback，不作为公开生产服务部署；此边界不意味着全部依赖问题已解决。

## 复核

```bash
npm --prefix frontend audit
npm --prefix frontend ls source-map-js braces postcss-selector-parser
```

公告和版本会变化，应核对当次输出的 advisory 链接及官方维护记录。后续可单独规划 Tailwind／PostCSS 迁移，回归视觉、构建与测试后再更新记录。

本次 npm audit 返回的公告：[braces](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)、[postcss-selector-parser](https://github.com/advisories/GHSA-rj75-hqrm-r3gf)。
