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
    des TEXT
)
""")

# 创建 categories 表，包含唯一的类别 id 和名称
cursor.execute("""
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE
)
""")

# 创建 app_categories 表，用于连接 apps 和 categories
cursor.execute("""
CREATE TABLE IF NOT EXISTS app_categories (
    appId TEXT,
    category_id INTEGER,
    FOREIGN KEY (appId) REFERENCES apps (appId),
    FOREIGN KEY (category_id) REFERENCES categories (id),
    PRIMARY KEY (appId, category_id)
)
""")

# 插入版本信息到 config 表
cursor.execute("INSERT INTO config (version) VALUES (?)", (data['version'],))

# 插入应用信息到 apps 表和类别信息到 categories 表
for app in data['apps']:
    # 插入应用基本信息到 apps 表
    cursor.execute("""
    INSERT OR IGNORE INTO apps (appId, name, user, repositories, icon, des) VALUES (?, ?, ?, ?, ?, ?)
    """, (app['appId'], app['name'], app['user'], app['repositories'], app['icon'], app['des']))

    # 处理类别，将每个类别插入 categories 表，并建立关联
    for category_name in app.get('category', []):
        # 插入类别到 categories 表，如果已存在则忽略
        cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category_name,))
        
        # 获取该类别的 id
        cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        category_id = cursor.fetchone()[0]
        
        # 插入到 app_categories 表，建立应用和类别的关联
        cursor.execute("""
        INSERT OR IGNORE INTO app_categories (appId, category_id) VALUES (?, ?)
        """, (app['appId'], category_id))

# 提交事务并关闭连接
conn.commit()
conn.close()

print("Version, app data, and categories with relationships have been successfully written to the SQLite database.")
