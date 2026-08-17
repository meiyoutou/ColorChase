import os


os.environ.setdefault(
    "COLORCHASE_DATABASE_URL",
    "mysql+aiomysql://colorchase:password@127.0.0.1:3306/colorchase_test",
)
