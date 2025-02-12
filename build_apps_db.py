import sqlite3
import yaml
import json
import os

# 读取 db.json 文件
with open("db.json", "r", encoding="utf-8") as file:
    db_data = json.load(file)
    app_version = db_data['app']['version']
    proxy_url = db_data['proxy']['url']

# 读取 category 数据
with open("sources/category.yaml", "r", encoding="utf-8") as file:
    category_data = yaml.safe_load(file)

# 连接到 SQLite 数据库（如果不存在会自动创建）
conn = sqlite3.connect("apps.db")
cursor = conn.cursor()

# 创建 config 表用于存储版本信息
cursor.execute("""
CREATE TABLE IF NOT EXISTS config (
    version TEXT PRIMARY KEY,
    proxy TEXT
)
""")

# 创建 apps 表，将 appId 设为主键
cursor.execute("""
CREATE TABLE IF NOT EXISTS apps (
    appId TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    user TEXT,
    repositories TEXT,
    icon TEXT,
    des TEXT,
    category TEXT
)
""")

# 创建 category 表，包含唯一的类别 id 和名称
cursor.execute("""
CREATE TABLE IF NOT EXISTS category (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    icon TEXT NOT NULL
)
""")

# 插入版本信息到 config 表
cursor.execute("INSERT OR REPLACE INTO config (version, proxy) VALUES (?,?)", (app_version, proxy_url))

# 插入 category 数据
for cat in category_data['category']:
    # 使用 ',' 分割 id 和 description
    cat_id, cat_description, icon = cat.split(',')
    cursor.execute("""
    INSERT OR REPLACE INTO category (id, description, icon) VALUES (?, ?, ?)
    """, (cat_id, cat_description, icon))

# 读取并插入应用数据
apps_dir = "sources/apps"
for filename in os.listdir(apps_dir):
    if filename.endswith(".yaml"):
        with open(os.path.join(apps_dir, filename), "r", encoding="utf-8") as file:
            app = yaml.safe_load(file)
            category_str = ','.join(app.get('category', []))
            # 插入应用基本信息到 apps 表
            cursor.execute("""
            INSERT OR REPLACE INTO apps (appId, name, user, repositories, icon, des, category) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (app['appId'], app['name'], app['user'], app['repositories'], app['icon'], app['des'], category_str))

# 提交事务并关闭连接
conn.commit()
conn.close()

print("Version, app data, and categories with relationships have been successfully written to the SQLite database.")
