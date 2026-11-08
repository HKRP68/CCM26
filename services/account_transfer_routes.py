"""Password-admin account recovery UI; not exposed to player authentication."""
import secrets
import json

from flask import (current_app, flash, redirect, render_template, request,
                   session, url_for)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

from models import AdminLog
from services import account_transfer_service as service


def register(app, login_required, get_session):
    @app.after_request
    def transfer_cache_headers(response):
        if request.endpoint == "account_transfer":
            response.headers["Cache-Control"] = "no-store"
        return response

    def signer():
        return URLSafeTimedSerializer(current_app.secret_key, salt="account-transfer-v1")

    @app.route("/users/transfer", methods=["GET", "POST"])
    @login_required
    def account_transfer():
        db = get_session()
        preview = token = None
        old = request.form.get("old_id", request.args.get("old_id", ""))
        new = request.form.get("new_id", "")
        reason = request.form.get("reason", "").strip()
        status = 200
        try:
            if request.method == "POST":
                action = request.form.get("action")
                if action == "preview":
                    session.pop("account_transfer_nonce", None)
                    if not 10 <= len(reason) <= 200:
                        raise service.TransferError("Record how ownership was verified (10–200 characters).")
                    user, preview, digest, _ = service.prepare(db, old, new)
                    nonce = secrets.token_urlsafe(24)
                    session["account_transfer_nonce"] = nonce
                    token = signer().dumps(dict(old=old, new=new, uid=user.id, digest=digest,
                                               reason=reason, nonce=nonce,
                                               destination_id=(preview["destination"]["user_id"]
                                                               if preview["destination"] else None)))
                elif action == "confirm":
                    try:
                        data = signer().loads(request.form.get("transfer_token", ""), max_age=600)
                    except (BadSignature, SignatureExpired):
                        raise service.TransferError("Preview expired or is invalid. Create a new preview.") from None
                    nonce = session.get("account_transfer_nonce")
                    if not nonce or not secrets.compare_digest(nonce, data["nonce"]):
                        raise service.TransferError("This preview has already been used or replaced. Create a new preview.")
                    if request.form.get("ownership_verified") != "yes":
                        raise service.TransferError("Verify that the old and new Telegram accounts belong to the same person.")
                    if request.form.get("confirm_new_id", "").strip() != str(service.telegram_id(data["new"])):
                        raise service.TransferError("Type the new Telegram ID again to confirm the destination.")
                    replacing = data.get("destination_id") is not None
                    if replacing and request.form.get("replace_destination") != "yes":
                        raise service.TransferError("Confirm replacement of the new ID's existing game account. Its data will be archived.")
                    user, receipt = service.transfer(db, data["old"], data["new"],
                        expected_user_id=data["uid"], expected_digest=data["digest"],
                        reason=data["reason"], ip_address=request.remote_addr,
                        replace_destination=replacing)
                    uid, receipt_id = user.id, receipt.id
                    db.commit()
                    session.pop("account_transfer_nonce", None)
                    archive_note = (f' Replaced account #{data["destination_id"]} saved as a disabled archive.'
                                    if replacing else "")
                    flash(f'Transfer complete: {data["old"]} → {data["new"]}. Squad, balances and progress preserved. Receipt #{receipt_id}.{archive_note} The player can now send /start from the new Telegram account.', "success")
                    db.close()
                    return redirect(url_for("user_detail", user_id=uid))
                else:
                    raise service.TransferError("Choose Preview transfer before confirming.")
        except service.TransferError as error:
            db.rollback()
            preview = token = None
            flash(str(error), "error")
            status = 400
        except IntegrityError:
            db.rollback()
            preview = token = None
            flash("The destination has conflicting saved records or was registered during transfer. No changes were saved. Resolve the conflict and preview again.", "error")
            status = 409
        except Exception:
            db.rollback()
            current_app.logger.exception("Account transfer failed")
            preview = token = None
            flash("Transfer unavailable. No changes were saved. Try again shortly.", "error")
            status = 503
        try:
            history = db.query(AdminLog).filter_by(action="account_transfer").order_by(AdminLog.id.desc()).limit(25).all()
            for entry in history:
                try:
                    entry.transfer_details = json.loads(entry.detail)
                except (TypeError, ValueError):
                    entry.transfer_details = None
            return render_template("admin_account_transfer.html", preview=preview, transfer_token=token,
                                   old_id=old, new_id=new, reason=reason, history=history), status
        finally:
            db.close()
