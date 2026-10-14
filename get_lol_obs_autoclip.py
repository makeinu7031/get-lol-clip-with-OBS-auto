"""LoL 自動クリップ保存 (OBS リプレイバッファ連携)

試合中に Live Client Data API を監視し、自分のキル/アシスト/マルチキルなどが
起きたら、OBS のリプレイバッファを保存する。
"""
import json, time, datetime, urllib3
import requests
import obsws_python as obs

# ===== 設定: config.json で変更できる(初回起動時に自動作成) =====
import os, sys, shutil, re
DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(DIR, "config.json")
DEFAULT = {
    "obs_host": "localhost", "obs_port": 4455, "obs_password": "",
    "my_name": "",           # 空なら自動取得。動かない時は ゲーム名(タグなし)
    "post_sec": 4,           # 最後のイベントから何秒後に保存するか
    "save_assist": True,     # アシストでも保存
    "save_objectives": True, # 自チームのバロン/ドラゴン/エースで保存
    "split_by_match": True,  # 試合ごとのフォルダに分けて保存
}
cfg = dict(DEFAULT)
if os.path.exists(CFG_PATH):
    cfg.update(json.load(open(CFG_PATH, encoding="utf-8")))
else:
    print("初回設定です。OBSの「ツール > WebSocketサーバー設定」のパスワードを入力してください")
    cfg["obs_password"] = input("OBSパスワード: ").strip()
    json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("config.json を保存しました")
OBS_HOST, OBS_PORT, OBS_PASSWORD = cfg["obs_host"], cfg["obs_port"], cfg["obs_password"]
MY_NAME, POST_SEC = cfg["my_name"], cfg["post_sec"]
SAVE_ASSIST, SAVE_OBJECTIVES = cfg["save_assist"], cfg["save_objectives"]
SPLIT = cfg["split_by_match"]
LOG_FILE = os.path.join(DIR, "clips_log.jsonl")
POLL = 0.5
# =================================================================

urllib3.disable_warnings()
BASE = "https://127.0.0.1:2999/liveclientdata/"
S = requests.Session(); S.verify = False

def get(path):
    r = S.get(BASE + path, timeout=2)
    data = r.json()
    if r.status_code != 200 or (isinstance(data, dict) and "errorCode" in data):
        raise ValueError(f"{path}: {data}")  # 試合準備中などは待機扱いにする
    return data

def short(n):  # "名前#タグ" -> "名前"
    return (n or "").split("#")[0]

def my_name():
    return short(MY_NAME or get("activeplayername"))

def my_team(me):
    for p in get("playerlist"):
        if short(p.get("riotIdGameName") or p.get("summonerName")) == me:
            return p.get("team")

def match_name(me):
    """試合フォルダ名: 開始日時_チャンピオン"""
    now = datetime.datetime.now()
    try: start = now - datetime.timedelta(seconds=get("gamestats")["gameTime"])
    except Exception: start = now
    champ = ""
    try:
        for p in get("playerlist"):
            if short(p.get("riotIdGameName") or p.get("summonerName")) == me:
                champ = p.get("championName", "")
    except Exception: pass
    return re.sub(r'[\\/:*?"<>|]', "", f"{start:%Y-%m-%d_%H%M}_{champ}").strip("_")

def move_clip(path, folder, n, labels):
    """保存されたクリップを 試合フォルダ/01_キル_07m03s.mp4 の形に移動"""
    dst_dir = os.path.join(os.path.dirname(path), folder); os.makedirs(dst_dir, exist_ok=True)
    names = "+".join(dict.fromkeys(l for l, _ in labels)); t = int(labels[0][1])
    name = re.sub(r'[\\/:*?"<>|]', "", f"{n:02d}_{names}_{t//60:02d}m{t%60:02d}s") + os.path.splitext(path)[1]
    dst = os.path.join(dst_dir, name)
    for _ in range(10):
        try: shutil.move(path, dst); return dst
        except OSError: time.sleep(0.5)
    return path

def judge(e, me, team_cache):
    """イベントを保存対象か判定。(ラベル) or None"""
    n = e["EventName"]
    if n == "ChampionKill":
        if short(e.get("KillerName")) == me: return "キル"
        if SAVE_ASSIST and me in [short(a) for a in e.get("Assisters", [])]: return "アシスト"
    elif n == "Multikill" and short(e.get("KillerName")) == me:
        return {2: "ダブル", 3: "トリプル", 4: "クアドラ", 5: "ペンタ"}.get(e.get("KillStreak"), "マルチ") + "キル"
    elif SAVE_OBJECTIVES and n in ("DragonKill", "BaronKill", "Ace"):
        if n == "Ace":
            return "エース" if e.get("AcingTeam") == team_cache() else None
        k = short(e.get("KillerName"))
        for p in get("playerlist"):
            if short(p.get("riotIdGameName") or p.get("summonerName")) == k:
                return ("バロン" if n == "BaronKill" else "ドラゴン") if p.get("team") == team_cache() else None
    return None

def main():
    cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
    if not cl.get_replay_buffer_status().output_active:
        cl.start_replay_buffer(); print("リプレイバッファを開始しました")
    print("OBS接続OK。LoLの試合開始を待っています... (Ctrl+Cで終了)")
    seen, pending, labels, me, team, last_msg = -1, None, [], None, None, ""
    folder, count = "", 0
    while True:
        try:
            if me is None:
                me = my_name(); team = my_team(me)
                folder = match_name(me); count = 0
                print(f"プレイヤー: {me} / チーム: {team} / フォルダ: {folder}"); seen = -1
            events = get("eventdata").get("Events", [])
            if events and events[-1]["EventID"] < seen: seen = -1; me = None; continue  # 新しい試合
            for e in events:
                if e["EventID"] <= seen: continue
                seen = e["EventID"]
                lab = judge(e, me, lambda: team)
                if lab:
                    labels.append((lab, round(e["EventTime"], 1))); pending = time.time()
                    print(f"[{e['EventTime']:.0f}s] {lab} を検出")
            if pending and time.time() - pending >= POST_SEC:
                cl.save_replay_buffer(); time.sleep(1.0)
                path = cl.get_last_replay_buffer_replay().saved_replay_path
                count += 1
                if SPLIT: path = move_clip(path, folder, count, labels)
                rec = {"saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                       "file": path, "events": labels}
                open(LOG_FILE, "a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")
                print("保存:", path); pending, labels = None, []
        except (requests.exceptions.RequestException, ValueError, AttributeError) as ex:
            me, pending, labels = None, None, []   # 試合外: 待機
            msg = str(ex)[:120]
            if msg != last_msg:
                print("待機中:", msg); last_msg = msg
            time.sleep(3); continue
        time.sleep(POLL)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("終了")
    except Exception as ex:
        print("\nエラー:", ex)
        print("OBSが起動していて、WebSocketが有効か、パスワードが合っているか確認してください。")
        print("パスワードを直す場合は config.json の obs_password を編集(またはファイルを削除して再実行)。")
        input("Enterキーで閉じます")
