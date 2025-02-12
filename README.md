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
