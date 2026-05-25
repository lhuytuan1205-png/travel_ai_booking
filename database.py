from flask_mysqldb import MySQL
from dotenv import load_dotenv
import os

load_dotenv()

mysql = MySQL()


def init_db(app):
    app.config["MYSQL_HOST"] = os.getenv("MYSQL_HOST")
    app.config["MYSQL_USER"] = os.getenv("MYSQL_USER")
    app.config["MYSQL_PASSWORD"] = os.getenv("MYSQL_PASSWORD")
    app.config["MYSQL_DB"] = os.getenv("MYSQL_DB")
    app.config["MYSQL_PORT"] = int(os.getenv("MYSQL_PORT", 3306))

    # Bật SSL khi dùng MySQL cloud như Aiven
    if os.getenv("MYSQL_SSL", "false").lower() == "true":
        app.config["MYSQL_SSL"] = {"ssl": {}}

    mysql.init_app(app)