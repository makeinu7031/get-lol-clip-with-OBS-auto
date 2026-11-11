"""LoL 自動クリップ保存 (OBS リプレイバッファ連携)

試合中に Live Client Data API を監視し、自分のキル/アシスト/マルチキルなどが
起きたら、OBS のリプレイバッファを保存する。
- キル後: 最後の検知から post_sec_* 秒後に保存
- キル前: pre_sec_* で「検知より前に入れたい秒数」を指定（全体は pre + post）
"""
import json, time, datetime, urllib3, subprocess, shutil
import requests
import obsws_python as obs

# ===== 設定: config.json で変更できる(初回起動時に自動作成) =====
import os, sys, shutil, re
DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(DIR, "config.json")
DEFAULT = {
    "obs_host": "localhost", "obs_port": 4455, "obs_password": "",
    "my_name": "",           # 空なら自動取得。動かない時は ゲーム名(タグなし)
    "post_sec_single": 15,   # 単キル: 最後の検知から何秒後に保存（キル後）
    "post_sec_multi": 15,    # マルチ: 最後の検知から何秒後に保存（キル後）
    "pre_sec_single": 15,    # 単キル: 検知より前に入れたい秒数
    "pre_sec_multi": 45,     # マルチ: 最後の検知より前に入れたい秒数
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
MY_NAME = cfg["my_name"]
POST_SEC_SINGLE = cfg.get("post_sec_single", 15)
POST_SEC_MULTI = cfg.get("post_sec_multi", 15)

def _read_pre_sec(multi: bool) -> int:
    """pre_sec_* を読む。旧 replay_sec_* は replay - post として互換"""
    key_pre = "pre_sec_multi" if multi else "pre_sec_single"
    key_post = "post_sec_multi" if multi else "post_sec_single"
    key_replay = "replay_sec_multi" if multi else "replay_sec_single"
    default_pre = 45 if multi else 15
    post = cfg.get(key_post, 15)
    if key_pre in cfg:
        return int(cfg[key_pre])
    if key_replay in cfg:
        return max(0, int(cfg[key_replay]) - int(post))
    return default_pre

PRE_SEC_SINGLE = _read_pre_sec(False)
PRE_SEC_MULTI = _read_pre_sec(True)
CLIP_SEC_SINGLE = PRE_SEC_SINGLE + POST_SEC_SINGLE
CLIP_SEC_MULTI = PRE_SEC_MULTI + POST_SEC_MULTI
SAVE_ASSIST, SAVE_OBJECTIVES = cfg["save_assist"], cfg["save_objectives"]
TRIM_WARNED = False
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

def is_multi_label(lab):
    return lab and any(x in lab for x in ("ダブル", "トリプル", "クアドラ", "ペンタ", "マルチ"))

def obs_replay_profile_section(cl):
    """出力モードに応じたプロファイルのカテゴリ (Simple / Advanced)"""
    try:
        r = cl.get_profile_parameter("Output", "Mode")
        mode = getattr(r, "parameter_value", None) or getattr(r, "parameterValue", "")
        return "AdvOut" if mode == "Advanced" else "SimpleOutput"
    except Exception:
        return "SimpleOutput"

def apply_obs_replay_seconds(cl, seconds, restart=True):
    """OBS のリプレイバッファ最大秒数 (RecRBTime) を設定"""
    sec = str(int(seconds))
    cat = obs_replay_profile_section(cl)
    active = cl.get_replay_buffer_status().output_active
    if active and restart:
        cl.stop_replay_buffer()
    cl.set_profile_parameter(cat, "RecRBTime", sec)
    if active and restart:
        cl.start_replay_buffer()

def trim_clip_tail(path, keep_sec):
    """保存ファイルの末尾 keep_sec 秒だけ残す（単キル用）"""
    global TRIM_WARNED
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        if not TRIM_WARNED:
            print(f"注意: ffmpeg 未検出のため単キル {keep_sec}s への切り詰めをスキップします（PATH に ffmpeg を追加可）")
            TRIM_WARNED = True
        return path
    tmp = path + ".trim.tmp" + os.path.splitext(path)[1]
    r = subprocess.run(
        [ffmpeg, "-y", "-sseof", f"-{int(keep_sec)}", "-i", path, "-c", "copy", tmp],
        capture_output=True,
    )
    if r.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        return path
    os.replace(tmp, path)
    return path

def main():
    cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
    replay_boot = max(CLIP_SEC_SINGLE, CLIP_SEC_MULTI)
    try:
        apply_obs_replay_seconds(cl, replay_boot, restart=True)
        print(f"OBSリプレイ最大時間を {replay_boot}s に設定しました")
    except Exception as ex:
        print(f"注意: OBSリプレイ秒数の自動設定に失敗 ({ex}) — 手動で {replay_boot}s 以上を推奨")
    if not cl.get_replay_buffer_status().output_active:
        cl.start_replay_buffer(); print("リプレイバッファを開始しました")
    print("OBS接続OK。LoLの試合開始を待っています... (Ctrl+Cで終了)")
    print(
        f"設定: 単=検知の前{PRE_SEC_SINGLE}s+後{POST_SEC_SINGLE}s / "
        f"マ=前{PRE_SEC_MULTI}s+後{POST_SEC_MULTI}s (OBSバッファ {replay_boot}s)"
    )
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
                    labels.append((lab, round(e["EventTime"], 1)))
                    pending = time.time()
                    # マルチなら長め、1キルなら短め。続いたら最後のタイマーを更新
                    if is_multi_label(lab):
                        current_post = POST_SEC_MULTI
                    else:
                        # 既存のマルチシリーズがあれば長めを優先
                        if any(is_multi_label(l) for l, _ in labels):
                            current_post = POST_SEC_MULTI
                        else:
                            current_post = POST_SEC_SINGLE
                    print(f"[{e['EventTime']:.0f}s] {lab} を検出 (保存まで{current_post}s)")
            if pending and time.time() - pending >= current_post:
                is_multi_clip = any(is_multi_label(l) for l, _ in labels)
                clip_len = CLIP_SEC_MULTI if is_multi_clip else CLIP_SEC_SINGLE
                pre_sec = PRE_SEC_MULTI if is_multi_clip else PRE_SEC_SINGLE
                cl.save_replay_buffer(); time.sleep(1.0)
                path = cl.get_last_replay_buffer_replay().saved_replay_path
                if not is_multi_clip and CLIP_SEC_SINGLE < replay_boot:
                    path = trim_clip_tail(path, CLIP_SEC_SINGLE)
                count += 1
                if SPLIT: path = move_clip(path, folder, count, labels)
                rec = {"saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                       "file": path, "events": labels, "post_sec": current_post,
                       "pre_sec": pre_sec, "clip_sec": clip_len, "multi": is_multi_clip}
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
