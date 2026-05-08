"""
dropbox_uploader.py — 透過 Dropbox API 上傳 epub 到 Kobo 同步資料夾

伺服器版替代 shutil.copy2 到本地 Dropbox 資料夾的做法。
讀取 .env 中的 DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN，
自動處理 access token 換發。
"""

import os
from pathlib import Path
from typing import Optional


# Kobo 期望的 Dropbox 資料夾路徑（雲端路徑，不分 Mac/Linux）
# 中文 Dropbox 預設是「應用程式」，英文是「Apps」。透過環境變數覆蓋。
KOBO_REMOTE_DIR = os.environ.get(
    "DROPBOX_KOBO_REMOTE_DIR", "/應用程式/Rakuten Kobo"
)


def _get_client():
    """每次呼叫都建立一個新的 Dropbox client（refresh token 模式）。"""
    import dropbox

    app_key = os.environ.get("DROPBOX_APP_KEY")
    app_secret = os.environ.get("DROPBOX_APP_SECRET")
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN")

    if not all([app_key, app_secret, refresh_token]):
        raise RuntimeError(
            "缺少 Dropbox 設定。請確認 .env 有 DROPBOX_APP_KEY / "
            "DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN"
        )

    return dropbox.Dropbox(
        app_key=app_key,
        app_secret=app_secret,
        oauth2_refresh_token=refresh_token,
    )


def upload_to_kobo(local_epub: str, remote_filename: Optional[str] = None) -> str:
    """把本地 epub 上傳到 Dropbox Kobo 同步資料夾。

    Args:
        local_epub: 本地 epub 路徑
        remote_filename: 雲端檔名（None = 用 local 的檔名）

    Returns:
        雲端完整路徑（例如 /應用程式/Rakuten Kobo/xxx.epub）
    """
    import dropbox
    from dropbox.files import WriteMode

    local_path = Path(local_epub)
    if not local_path.exists():
        raise FileNotFoundError(f"找不到 epub: {local_epub}")

    name = remote_filename or local_path.name
    remote_path = f"{KOBO_REMOTE_DIR}/{name}"

    dbx = _get_client()
    with open(local_path, "rb") as f:
        data = f.read()

    print(f"📤 Dropbox 上傳中... ({len(data) // 1024} KB → {remote_path})")
    dbx.files_upload(
        data,
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,  # 不在 Dropbox 對 user 發通知
    )
    print(f"✅ 已上傳到 Dropbox: {remote_path}")
    return remote_path


def list_kobo_files(folder: Optional[str] = None):
    """列出 Kobo 同步資料夾裡的檔案（預設只列 epub/kepub）。

    Returns:
        list of dict: {name, path, size, modified}
    """
    folder = folder or KOBO_REMOTE_DIR
    dbx = _get_client()
    out = []
    try:
        res = dbx.files_list_folder(folder)
    except Exception as e:
        raise RuntimeError(f"讀取 Dropbox 失敗：{e}")

    while True:
        for entry in res.entries:
            # 只取檔案（避免列出子資料夾）
            if not hasattr(entry, "size"):
                continue
            name = entry.name
            if not (name.lower().endswith(".epub") or name.lower().endswith(".kepub.epub")):
                continue
            out.append({
                "name": name,
                "path": entry.path_lower,  # Dropbox 刪除用 path_lower 也 OK
                "path_display": entry.path_display,
                "size": entry.size,
                "modified": entry.client_modified.isoformat() if entry.client_modified else "",
            })
        if not res.has_more:
            break
        res = dbx.files_list_folder_continue(res.cursor)

    out.sort(key=lambda x: x["modified"], reverse=True)
    return out


def delete_kobo_file(remote_path: str) -> None:
    """刪除 Dropbox 上的單一檔案。"""
    dbx = _get_client()
    dbx.files_delete_v2(remote_path)


def is_configured() -> bool:
    """檢查 Dropbox API 環境變數是否齊全。"""
    return all(
        os.environ.get(k)
        for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
    )
