import sqlite3
import yaml

# 读取 YAML 文件内容
with open("apps.yaml", "r", encoding="utf-8") as file:
    data = yaml.safe_load(file)

# 连接到 SQLite 数据库（如果不存在会自动创建）
conn = sqlite3.connect("config.db")
cursor = conn.cursor()

# 创建 config 表用于存储版本信息
cursor.execute("""
CREATE TABLE IF NOT EXISTS config (
    version TEXT PRIMARY KEY
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

# 创建 categorie 表，包含唯一的类别 id 和名称
cursor.execute("""
CREATE TABLE IF NOT EXISTS category (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL
)
""")

# 插入版本信息到 config 表
cursor.execute("INSERT INTO config (version) VALUES (?)", (data['version'],))

# 插入 category 数据
for cat in data['category']:
    # 使用 ',' 分割 id 和 description
    cat_id, cat_description = cat.split(',')
    # 插入数据到 category 表
    cursor.execute("""
    INSERT OR IGNORE INTO category (id, description) VALUES (?, ?)
    """, (cat_id, cat_description))



# 插入应用信息到 apps 表和类别信息到 categories 表
for app in data['apps']:
    category_str = ','.join(app.get('category', []))
    # 插入应用基本信息到 apps 表
    cursor.execute("""
    INSERT OR IGNORE INTO apps (appId, name, user, repositories, icon, des,category) VALUES (?, ?, ?, ?, ?, ?,?)
    """, (app['appId'], app['name'], app['user'], app['repositories'], app['icon'], app['des'],category_str))


# 提交事务并关闭连接
conn.commit()
conn.close()

print("Version, app data, and categories with relationships have been successfully written to the SQLite database.")
