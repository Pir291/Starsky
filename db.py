import os
import mysql.connector
from mysql.connector import Error
from typing import List, Dict, Optional
import json


DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "sql10.freesqldatabase.com"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "sql10816934"),
    "password": os.environ.get("DB_PASSWORD", "zXg6nD6AAF"),
    "database": os.environ.get("DB_NAME", "sql10816934"),
}



def get_connection():
    return mysql.connector.connect(**DB_CONFIG)



# ====== USERS ======

def create_or_update_user(telegram_id: int, username: Optional[str], info: Optional[str]):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (telegram_id, username, info, last_activity)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              username = VALUES(username),
              info = VALUES(info),
              last_activity = NOW()
            """,
            (telegram_id, username, info)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def update_last_activity(telegram_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET last_activity = NOW() WHERE telegram_id = %s",
            (telegram_id,)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_all_users_from_db() -> List[Dict]:
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT telegram_id, username, info, last_activity FROM users")
        rows = cur.fetchall()
        return rows
    finally:
        cur.close()
        conn.close()


def update_info_in_db(telegram_id: int, info: str):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET info = %s WHERE telegram_id = %s",
            (info, telegram_id)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict]:
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT telegram_id, username, info, last_activity FROM users WHERE telegram_id = %s",
            (telegram_id,)
        )
        row = cur.fetchone()
        return row
    finally:
        cur.close()
        conn.close()


def set_login_code(telegram_id: int, code: Optional[str]):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET login_code = %s WHERE telegram_id = %s",
            (code, telegram_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ====== ПУБЛИЧНЫЙ ЧАТ ======

def save_public_message(user_id: int, username: str, text: str):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (user_id, username, text) VALUES (%s, %s, %s)",
            (user_id, username, text)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_public_messages(limit: int = 200) -> List[Dict]:
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT user_id, username, text, created_at
            FROM chat_messages
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (limit,)
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


# ====== СТАТУС ЗВЁЗД (user_stars) ======

def get_star_state(user_id: int) -> Optional[Dict]:
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT user_id, activity_score, star_color, star_shape, info, skins_owned
            FROM user_stars
            WHERE user_id = %s
            """,
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        skins_raw = row.get("skins_owned")
        if skins_raw is None:
            skins = []
        else:
            if isinstance(skins_raw, str):
                try:
                    skins = json.loads(skins_raw)
                except Exception:
                    skins = []
            else:
                skins = skins_raw
        return {
            "user_id": row["user_id"],
            "activity_score": float(row["activity_score"]),
            "star_color": row["star_color"],
            "star_shape": row["star_shape"],
            "info": row.get("info") or "",
            "skins_owned": skins,
        }
    finally:
        cur.close()
        conn.close()


def upsert_star_state(
    user_id: int,
    activity_score: float,
    star_color: str,
    star_shape: str,
    info: str,
    skins_owned: Optional[list],
):
    if skins_owned is None:
        skins_owned = []
    skins_json = json.dumps(skins_owned, ensure_ascii=False)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_stars (user_id, activity_score, star_color, star_shape, info, skins_owned)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              activity_score = VALUES(activity_score),
              star_color     = VALUES(star_color),
              star_shape     = VALUES(star_shape),
              info           = VALUES(info),
              skins_owned    = VALUES(skins_owned)
            """,
            (user_id, activity_score, star_color, star_shape, info, skins_json)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_all_star_states() -> List[Dict]:
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT user_id, activity_score, star_color, star_shape, info, skins_owned
            FROM user_stars
            """
        )
        rows = cur.fetchall()
        result: List[Dict] = []
        for row in rows:
            skins_raw = row.get("skins_owned")
            if skins_raw is None:
                skins = []
            else:
                if isinstance(skins_raw, str):
                    try:
                        skins = json.loads(skins_raw)
                    except Exception:
                        skins = []
                else:
                    skins = skins_raw
            result.append(
                {
                    "user_id": row["user_id"],
                    "activity_score": float(row["activity_score"]),
                    "star_color": row["star_color"],
                    "star_shape": row["star_shape"],
                    "info": row.get("info") or "",
                    "skins_owned": skins,
                }
            )
        return result
    finally:
        cur.close()
        conn.close()
if __name__ == "__main__":
    try:
        conn = get_connection()
        print("Connected:", conn.is_connected())
        conn.close()
    except Error as e:
        print("DB error:", e)
