"""Activity log archival to Telegram storage channel.

How it works:
  1. Admin clicks "Archive logs older than N days"
  2. SELECT all activity_log rows with created_at < cutoff
  3. Dump to JSON file
  4. Upload file to Telegram storage channel via tg_storage_service
  5. Insert ActivityLogArchive row with file_id + date range + count
  6. DELETE the archived rows from activity_log

Net effect: a 1 MB JSON dump of (say) 10k rows lives in the channel for
free, and 10k Neon rows are gone. Admin can re-download the dump later
via the file_id if they need to investigate.

This service does NOT run on a schedule — it's admin-triggered only, so
the user controls when it happens (e.g. before redeploys, monthly).
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def estimate_archivable(session, older_than_days=30):
    """Quick count of how many rows would be archived if run now.

    Returns dict {row_count, oldest, newest}.
    """
    from models import ActivityLog
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    q = session.query(ActivityLog).filter(ActivityLog.created_at < cutoff)
    row_count = q.count()
    if row_count == 0:
        return {"row_count": 0, "oldest": None, "newest": None}
    oldest = session.query(func.min(ActivityLog.created_at)).filter(
        ActivityLog.created_at < cutoff).scalar()
    newest = session.query(func.max(ActivityLog.created_at)).filter(
        ActivityLog.created_at < cutoff).scalar()
    return {"row_count": row_count, "oldest": oldest, "newest": newest}


def archive_old_logs(session, older_than_days=30, archived_by=None):
    """Archive activity_log rows older than `older_than_days`.

    Returns dict with result:
      {ok: bool, archived: int, file_id: str | None,
       archive_id: int | None, message: str}
    """
    from models import ActivityLog, ActivityLogArchive
    from services import tg_storage_service

    if not tg_storage_service.is_configured():
        return {"ok": False, "archived": 0, "file_id": None,
                "archive_id": None,
                "message": "STORAGE_CHAT_ID not configured. Set it on Render first."}

    cutoff = datetime.utcnow() - timedelta(days=older_than_days)

    # Fetch rows to archive
    rows = (session.query(ActivityLog)
            .filter(ActivityLog.created_at < cutoff)
            .order_by(ActivityLog.created_at.asc())
            .all())
    if not rows:
        return {"ok": True, "archived": 0, "file_id": None,
                "archive_id": None,
                "message": f"No logs older than {older_than_days} days. Nothing to archive."}

    range_start = rows[0].created_at
    range_end = rows[-1].created_at

    # Serialize to JSON
    payload = [{
        "id": r.id,
        "user_id": r.user_id,
        "action": r.action,
        "detail": r.detail,
        "coins_change": r.coins_change,
        "gems_change": r.gems_change,
        "player_name": r.player_name,
        "player_rating": r.player_rating,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]

    # Write to temp file
    filename = f"activity_log_archive_{range_start.strftime('%Y%m%d')}_to_{range_end.strftime('%Y%m%d')}_{len(rows)}rows.json"
    tmpdir = tempfile.gettempdir()
    fpath = os.path.join(tmpdir, filename)
    try:
        with open(fpath, "w") as fp:
            json.dump(payload, fp, indent=1, default=str)
    except Exception as e:
        logger.exception("Failed to write archive file")
        return {"ok": False, "archived": 0, "file_id": None,
                "archive_id": None,
                "message": f"Failed to write archive file: {e}"}

    # Upload to Telegram channel
    try:
        caption = (f"📦 activity_log archive\n"
                   f"Range: {range_start.date()} → {range_end.date()}\n"
                   f"Rows: {len(rows)}")
        file_id = _upload_sync(fpath, caption=caption)
    except Exception:
        logger.exception("Channel upload failed")
        return {"ok": False, "archived": 0, "file_id": None,
                "archive_id": None,
                "message": "Channel upload failed. Logs NOT deleted from DB."}

    if not file_id:
        return {"ok": False, "archived": 0, "file_id": None,
                "archive_id": None,
                "message": "Upload returned no file_id. Logs NOT deleted from DB."}

    # Insert archive pointer row
    archive_row = ActivityLogArchive(
        range_start=range_start,
        range_end=range_end,
        row_count=len(rows),
        tg_file_id=file_id,
        filename=filename,
        archived_at=datetime.utcnow(),
        archived_by=archived_by,
    )
    session.add(archive_row)
    session.flush()
    archive_id = archive_row.id

    # Now safe to delete from DB — upload succeeded
    deleted_count = (session.query(ActivityLog)
                     .filter(ActivityLog.created_at < cutoff)
                     .delete(synchronize_session=False))
    session.commit()

    # Clean up temp file
    try:
        os.remove(fpath)
    except Exception:
        pass

    logger.info(f"Archived {deleted_count} activity_log rows (file_id={file_id[:30]}...)")
    return {"ok": True, "archived": deleted_count, "file_id": file_id,
            "archive_id": archive_id,
            "message": f"Archived {deleted_count} rows to Telegram channel."}


def _upload_sync(file_path, caption=None):
    """Sync wrapper for upload_document_async."""
    import asyncio
    from services.tg_storage_service import upload_document_async
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            upload_document_async(file_path, caption=caption))
    finally:
        loop.close()
