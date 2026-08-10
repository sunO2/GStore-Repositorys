# GStore-Repositorys
app 仓库配置文件
生成db文件

### 文件说明
#### sources：
1. category.yaml 文件用于分类表
2. apps 下每一个应用对应一个文件

#### db.json：
用于 [My JSON Server](https://my-json-server.typicode.com/suno2/GStore-Repositorys) 服务器提供代理接口获取api
仅仅更新 source 目录下任何文件都不会触发 Actions 自动构建 
需要更新db.json 才会触发Actions构建

#### build_apps_db.py：
用于生成 数据库脚本

## 应用元数据自动提取（App Metadata）

针对 **GitHub release 直接发布 APK** 的应用，仓库通过 GitHub Actions 自动提取其**真实应用名、包名、图标、versionName、versionCode**，存入 `metadata/` 目录，供 GStore 应用端使用（解决 GitHub API 渠道无真实图标/包名的问题）。

### 如何提交

1. 点击 **New issue**，选择 **"提交应用元数据 (App Metadata)"** 模板
2. 填写 **仓库地址**（格式 `https://github.com/owner/repo`，要求其最新 release 中直接附带 `.apk` 资产）
3. 可选填写 **APK 资产关键词**（如 `arm64-v8a` / `universal`；留空取第一个 `.apk`）
4. 提交后 Actions 自动处理：下载 APK → androguard 提取 → 写入 `metadata/` → 评论结果并关闭 issue

### 规则说明

- 标题必须以 `[app-metadata]` 开头（模板自动生成），否则不会被处理
- 数据写入 `metadata/{owner}@{repo}.yaml`，图标写入 `metadata/icons/{owner}@{repo}.png`
- 仅处理最新 release；仅接受 GitHub release 中的 APK（<200MB）
- 处理结果会在 issue 中评论（成功：提取的信息；失败：原因）

### 数据格式示例

```yaml
owner: termux
repo: termux-app
packageName: com.termux
appName: Termux
versionName: 0.119.0-beta.3
versionCode: 1022
icon: metadata/icons/termux@termux-app.png
sourceTag: v0.119.0-beta.3
sourceApk: termux-app_v0.119.0-beta.3+apt-android-5-github-debug_arm64-v8a.apk
generatedAt: "2025-05-22T22:48:54Z"
```

> 该目录数据暂未合入 `apps.db`，后续版本会合并进数据库更新链路。
