import os
import zipfile
import random
import string

# 配置
ROOT_DIR = "./messy_files"
ZIP_NAME = "messy_test.zip"
FILE_COUNT = 80  # 总文件数量
FOLDER_COUNT = 6  # 嵌套文件夹数量
EXTENSIONS = [
    "txt", "log", "json", "csv", "md", "py", "png", "jpg", "pdf", "docx",
    "xlsx", "tmp", "cache", "dat", "ini", "conf", "zip", "rar", "7z", ""
]
CHARS = string.ascii_letters + string.digits + "中文测试_"

def random_name():
    length = random.randint(4, 22)
    return "".join(random.choice(CHARS) for _ in range(length))

def make_files():
    os.makedirs(ROOT_DIR, exist_ok=True)
    # 创建子文件夹
    folders = [ROOT_DIR]
    for i in range(FOLDER_COUNT):
        fp = os.path.join(ROOT_DIR, f"folder_{random_name()}")
        os.makedirs(fp, exist_ok=True)
        folders.append(fp)
    # 生成一堆文件
    for _ in range(FILE_COUNT):
        folder = random.choice(folders)
        ext = random.choice(EXTENSIONS)
        fname = random_name()
        if ext:
            fname += "." + ext
        fpath = os.path.join(folder, fname)
        # 写入少量随机内容或者空
        with open(fpath, "w", encoding="utf-8", errors="ignore") as f:
            if random.random() > 0.3:
                f.write(f"random content {random.random()}\n")
    # 打包zip
    with zipfile.ZipFile(ZIP_NAME, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(ROOT_DIR):
            for fn in files:
                full = os.path.join(root, fn)
                arcname = os.path.relpath(full, ROOT_DIR)
                zf.write(full, arcname)
    print(f"✅ 打包完成：{ZIP_NAME}")
    print(f"一共 {FILE_COUNT} 个文件，{FOLDER_COUNT} 个嵌套目录")

if __name__ == "__main__":
    make_files()
