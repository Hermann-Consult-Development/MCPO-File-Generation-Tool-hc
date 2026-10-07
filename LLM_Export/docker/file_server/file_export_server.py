from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
import os
import pathlib

EXPORT_DIR_ENV = os.getenv("FILE_EXPORT_DIR")
EXPORT_DIR = (EXPORT_DIR_ENV or r"/output").rstrip("/")
REQUIRE_USER_AUTH = os.getenv("REQUIRE_USER_AUTH", "false").lower() == "true"
os.makedirs(EXPORT_DIR, exist_ok=True)

app = FastAPI()

@app.get("/files/{folder_name}/{filename}")
async def serve_file(folder_name: str, filename: str):
    if REQUIRE_USER_AUTH:
        # Secure exports live in the owning user's OWUI file store only.
        raise HTTPException(status_code=404, detail="Use the authenticated Open WebUI file link")
    root = pathlib.Path(EXPORT_DIR).resolve()
    if any(value in (".", "..") or "/" in value or "\\" in value for value in (folder_name, filename)):
        raise HTTPException(status_code=404, detail="File not found")
    file_path = (root / folder_name / filename).resolve()
    if root not in file_path.parents:
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=file_path,
        media_type='application/octet-stream',
        filename=filename,
    )

if not REQUIRE_USER_AUTH:
    app.mount("/files", StaticFiles(directory=EXPORT_DIR), name="files")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9003)
