import ast
import copy
import datetime
import difflib
import hashlib
import json
import logging
import os
import queue
import re
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

LOG_FILE = os.path.expanduser("~/Library/Logs/imessage_relay.log")
ADDRESS_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Address_list.md")
ANALYSIS_STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state_lens_analysis.json")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_ENABLED = bool(OPENAI_API_KEY) and OPENAI_API_KEY not in {"sk-...", "sk-xxxx", "sk-your-key"}
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "").strip()
OPENAI_MODEL_LIGHT = os.environ.get("OPENAI_MODEL_LIGHT", "").strip() or OPENAI_MODEL or "gpt-5-mini"
OPENAI_MODEL_HEAVY = os.environ.get("OPENAI_MODEL_HEAVY", "").strip() or "gpt-5.2"
OPENAI_TIMEOUT_SEC = float(os.environ.get("OPENAI_TIMEOUT", os.environ.get("OPENAI_TIMEOUT_SEC", "15")))
OPENAI_RETRIES = int(os.environ.get("OPENAI_MAX_RETRIES", os.environ.get("OPENAI_RETRIES", "2")))
SELF_DISPLAY_NAME = os.environ.get("SELF_DISPLAY_NAME", "西岡まひろ").strip() or "西岡まひろ"

ANALYSIS_LOCK = threading.Lock()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_log_timestamp(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def _normalize_phone(value):
    compact = re.sub(r"[\s\-()]", "", value)
    if not compact:
        return set()

    if compact.startswith("+"):
        plus_form = "+" + re.sub(r"\D", "", compact[1:])
    else:
        plus_form = re.sub(r"\D", "", compact)
    if not plus_form or plus_form == "+":
        return set()

    forms = {plus_form}
    if plus_form.startswith("+"):
        forms.add(plus_form[1:])

    digits_only = plus_form[1:] if plus_form.startswith("+") else plus_form
    if digits_only.startswith("0") and len(digits_only) >= 10:
        forms.add("+81" + digits_only[1:])
    if plus_form.startswith("+81") and len(plus_form) > 3:
        forms.add("0" + plus_form[3:])
    if digits_only.startswith("81") and len(digits_only) > 2:
        forms.add("0" + digits_only[2:])
    return forms


def _normalize_identifier(value):
    raw = str(value).strip().strip("'\"")
    if not raw:
        return set()
    if "@" in raw:
        return {raw.lower()}
    return _normalize_phone(raw)


def canonical_identifier(value):
    raw = str(value).strip().strip("'\"")
    if not raw:
        return ""
    if raw.lower() in {"me", "self"}:
        return "me"
    normalized = sorted(_normalize_identifier(raw))
    if normalized:
        return normalized[0]
    return raw.lower()


def parse_recipient_values(recipient_raw):
    raw = recipient_raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, (list, tuple)):
                return [str(v).strip().strip("'\"") for v in parsed if str(v).strip()]
        except Exception:
            pass
    if "," in raw:
        return [part.strip().strip("'\"") for part in raw.split(",") if part.strip()]
    cleaned = raw.strip().strip("'\"")
    return [cleaned] if cleaned else []


def load_address_book():
    mapping = {}
    if not os.path.exists(ADDRESS_LIST_FILE):
        return mapping

    current_name = None
    try:
        with open(ADDRESS_LIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("・"):
                    if not current_name:
                        continue
                    identifier = stripped.lstrip("・").strip()
                    mapping[identifier] = current_name
                    for normalized in _normalize_identifier(identifier):
                        mapping[normalized] = current_name
                else:
                    current_name = stripped
    except Exception as e:
        logging.warning(f"Failed to load address list: {e}")
    return mapping


ADDRESS_BOOK = load_address_book()


def resolve_contact(identifier):
    raw = str(identifier).strip().strip("'\"")
    if not raw:
        return raw
    if raw in ADDRESS_BOOK:
        return ADDRESS_BOOK[raw]
    for normalized in _normalize_identifier(raw):
        if normalized in ADDRESS_BOOK:
            return ADDRESS_BOOK[normalized]
    return raw


def resolve_recipient_field(recipient_raw):
    values = parse_recipient_values(recipient_raw)
    if not values:
        return ""
    return ", ".join(resolve_contact(v) for v in values)


def clean_message_content(content):
    original = str(content or "")
    text = original
    has_metadata_marker = (
        "__kIM" in original
        or "bplist00" in original
        or "NSKeyedArchiver" in original
        or "DDScannerResult" in original
    )
    has_attachment_marker = (
        "__kIMFileTransferGUIDAttributeName" in original
        or "\uFFFC" in original
    )

    text = text.replace("\uFFFC", "")
    text = re.sub(r"(?s)(?:__kIM[A-Za-z0-9_]*|bplist00).*$", "", text)
    text = re.sub(
        r"(?s)(?:_NSKeyedArchiver|NSKeyedArchiver|NSMutableDataNSData|_NS\.rangeval|DDScannerResult).*?$",
        "",
        text,
    )

    text = re.sub(r"^\s*@\+\??\s*", "", text)
    text = re.sub(r"(?:[iIl1][ \t]*){3,}['\"]?\s*$", "", text)

    cleaned = text.strip()
    if has_metadata_marker and cleaned.startswith("0") and len(cleaned) > 1:
        if re.match(r"[ぁ-んァ-ン一-龥々ー]", cleaned[1]):
            cleaned = cleaned[1:]
    if not cleaned and has_attachment_marker:
        return "[画像]"
    return cleaned


def load_analysis_store():
    if not os.path.exists(ANALYSIS_STORE_FILE):
        return {"messages": {}}
    try:
        with open(ANALYSIS_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"messages": {}}
            data.setdefault("messages", {})
            return data
    except Exception as e:
        logging.warning(f"Failed to load analysis store: {e}")
        return {"messages": {}}


def save_analysis_store(store):
    tmp_file = ANALYSIS_STORE_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp_file, ANALYSIS_STORE_FILE)
    except Exception as e:
        logging.warning(f"Failed to save analysis store: {e}")


ANALYSIS_STORE = load_analysis_store()


def sentiment_fallback(text):
    text_n = (text or "").lower()
    positive_words = ["最高", "嬉しい", "ありがとう", "笑", "楽しい", "好き", "良い", "いいね", "わくわく", "助かる", "😄", "😊", "✨", "❤️", "👍", "お疲れ"]
    negative_words = ["最悪", "悲しい", "辛い", "苦しい", "嫌", "ダメ", "面倒", "疲れた", "😭", "😢", "👎", "ごめん", "無理"]
    angry_words = ["怒", "ムカつく", "ふざけ", "💢", "😡", "最低", "許さない"]
    anxious_words = ["不安", "心配", "どうしよう", "やばい", "困る", "焦る"]
    urgent_words = ["急ぎ", "至急", "いますぐ", "今すぐ", "早く", "締切", "期限", "提出", "deadline", "due"]
    affectionate_words = ["大好き", "愛", "ありがとう", "おつかれ", "助かる", "嬉しい"]

    scores = {
        "positive": sum(text_n.count(w) for w in positive_words),
        "negative": sum(text_n.count(w) for w in negative_words),
        "angry": sum(text_n.count(w) for w in angry_words),
        "anxious": sum(text_n.count(w) for w in anxious_words),
        "urgent": sum(text_n.count(w) for w in urgent_words),
        "affectionate": sum(text_n.count(w) for w in affectionate_words),
    }
    if all(v == 0 for v in scores.values()):
        return "neutral", 0.55, "落ち着いた共有・連絡のトーンです。"
    top_label = max(scores.items(), key=lambda x: x[1])[0]
    top_score = min(1.0, 0.45 + (scores[top_label] * 0.15))
    label_map = {
        "positive": "positive",
        "negative": "negative",
        "angry": "negative",
        "anxious": "anxious",
        "urgent": "urgent",
        "affectionate": "affectionate",
    }
    nuance_map = {
        "positive": "前向きで協力的なニュアンスです。",
        "negative": "負担感や否定的なニュアンスが含まれます。",
        "angry": "強い不満や苛立ちがにじむニュアンスです。",
        "anxious": "不安や心配を含むニュアンスです。",
        "urgent": "急ぎ対応を求めるニュアンスです。",
        "affectionate": "親愛・感謝が強いニュアンスです。",
    }
    return label_map[top_label], top_score, nuance_map[top_label]


def detect_language_features(text):
    s = text or ""
    def has(p):
        return bool(re.search(p, s))

    return {
        "typo_detected": has(r"[ぁ-んァ-ン一-龥]{1,2}[a-zA-Z0-9]{1,2}[ぁ-んァ-ン一-龥]{1,2}") or has(r"(.)\1{4,}"),
        "typo_note": "入力の揺れ・打ち間違いの可能性あり" if has(r"[ぁ-んァ-ン一-龥]{1,2}[a-zA-Z0-9]{1,2}[ぁ-んァ-ン一-龥]{1,2}") or has(r"(.)\1{4,}") else "",
        "wordplay_detected": has(r"(笑|w{2,}|草|ダジャレ|ことば遊び|語呂)"),
        "wordplay_note": "語呂や砕けた言い回しが含まれます" if has(r"(笑|w{2,}|草|ダジャレ|ことば遊び|語呂)") else "",
        "question_expression": has(r"(してくれる|お願いします|お願い|頼む|もらえる|してほしい)"),
        "interrogative_expression": has(r"[?？]") or has(r"(なに|何|どこ|どう|いつ|なぜ|なんで)"),
        "strong_assertion_expression": has(r"(絶対|必ず|間違いない|断言)"),
        "request_expression": has(r"(お願い|してください|してほしい|頼む|下さい)"),
        "assertive_expression": has(r"(です|だ。|ます。|に違いない)"),
        "speculative_expression": has(r"(かも|かな|たぶん|おそらく|と思う)"),
        "impression_expression": has(r"(すごい|いいね|やばい|嬉しい|悲しい|最高|最悪)"),
        "confirmation_expression": has(r"(確認|合ってる|これで|OK|了解|だよね|でいい)"),
    }


TOPIC_HINTS = [
    ("課題・提出", r"(宿題|課題|提出|締切|期限|レポート|締め切り)"),
    ("予定調整", r"(予定|明日|来週|日時|何時|集合|会う)"),
    ("画像共有", r"(\[画像\]|写真|画像|スクショ)"),
    ("URL共有", r"(https?://)"),
    ("システム運用", r"(起動|停止|サーバ|エラー|ログ|権限|設定)"),
]


def infer_topic(text):
    s = text or ""
    for label, pattern in TOPIC_HINTS:
        if re.search(pattern, s, re.IGNORECASE):
            return label
    if not s.strip():
        return "不明"
    compact = re.sub(r"\s+", " ", s.strip())
    return compact[:12]


def infer_intent(text, features):
    s = text or ""
    if features["request_expression"]:
        return "request"
    if features["interrogative_expression"]:
        return "ask"
    if features["confirmation_expression"]:
        return "confirm"
    if re.search(r"(報告|共有|お知らせ|FYI|連絡)", s):
        return "report"
    if re.search(r"(不満|困る|無理|最悪|怒)", s):
        return "complain"
    if re.search(r"(予定|計画|しよう|しようか|やる)", s):
        return "plan"
    return "share"


INTENT_LABELS = {
    "request", "report", "ask", "confirm", "complain", "share", "plan", "emotional_expression"
}


def is_actionable_message(text, features):
    s = text or ""
    if features.get("request_expression") or features.get("confirmation_expression"):
        return True
    if re.search(r"(締切|期限|提出|までに|至急|急ぎ|対応|やって|して|送って|確認して|買ってきて|頼む|お願い)", s):
        return True
    return False


def normalize_intent_label(intent, text, features):
    key = str(intent or "").strip().lower()
    if key in INTENT_LABELS:
        return key
    return infer_intent(text, features)


def build_compact_summary(message, topic, intent):
    sender = str(message.get("sender", "")).strip()
    sender_name = SELF_DISPLAY_NAME if _is_me_name(sender) else sender
    topic_txt = topic if topic and topic != "不明" else "近況"
    intent_jp = {
        "request": "依頼",
        "report": "報告",
        "ask": "質問",
        "confirm": "確認",
        "complain": "不満表明",
        "share": "共有",
        "plan": "計画",
        "emotional_expression": "感情表現",
    }.get(intent, "共有")
    return f"{sender_name}が{topic_txt}について{intent_jp}している。"


def normalize_content_summary(summary, message, topic, intent):
    raw = re.sub(r"\s+", " ", str(summary or "")).strip()
    content = re.sub(r"\s+", " ", str(message.get("content", "") or "")).strip()
    if not raw:
        return build_compact_summary(message, topic, intent)
    if content:
        similarity = difflib.SequenceMatcher(a=raw, b=content).ratio()
        if similarity >= 0.72 or len(raw) > 110:
            return build_compact_summary(message, topic, intent)
    return raw[:110]


def normalize_task_candidates(tasks, message, features):
    if not isinstance(tasks, list):
        return []
    if not is_actionable_message(message.get("content", ""), features):
        return []
    out = []
    content = str(message.get("content", "") or "")
    for t in tasks:
        line = normalize_action_text(str(t), message)
        if not line:
            continue
        # 原文丸写しを抑制
        if content and difflib.SequenceMatcher(a=line, b=content).ratio() > 0.72:
            line = normalize_action_text(content, message)
        if not line.endswith("。"):
            line += "。"
        if line not in out:
            out.append(line[:100])
    return out[:6]


def infer_tasks(text):
    s = text or ""
    compact = re.sub(r"\s+", " ", s).strip()
    tasks = []

    if re.search(r"(して|してほしい|やって|送って|確認して|買ってきて|までに|締切|期限|提出|お願い|頼む|ください)", s):
        action = compact
        # 呼びかけを軽く除去
        action = re.sub(r"^[\wぁ-んァ-ン一-龥々ー]+(?:さん|くん|ちゃん)?[、,\s]+", "", action)
        if action:
            tasks.append(action[:90])

    if re.search(r"(締切|期限|提出|までに)", s):
        tasks.append("期限と提出物を確認する")
    if "URL" in s or re.search(r"https?://", s):
        tasks.append("共有URLを確認する")

    dedup = []
    for t in tasks:
        if t not in dedup:
            dedup.append(t)
    return dedup


def participant_aliases(name):
    n = str(name or "").strip()
    if not n:
        return set()
    aliases = {n}
    compact = re.sub(r"\s+", "", n)
    aliases.add(compact)
    compact = re.sub(r"(さん|くん|ちゃん|氏)$", "", compact)
    aliases.add(compact)
    if len(compact) >= 3:
        aliases.add(compact[-2:])
        aliases.add(compact[-3:])
    return {a for a in aliases if a}


NICKNAME_RULES = {
    "西岡千尋": [
        {"alias": "千尋"}, {"alias": "ちひろ"}, {"alias": "ちぃ"}, {"alias": "ちひろさん"},
        {"alias": "パパ"}, {"alias": "ぱぱ"}, {"alias": "ぱっぱ"}, {"alias": "ぱーぱー"}, {"alias": "ぱぱん"},
        {"alias": "おにいさん", "sender_in": {"西岡二記子", "西岡幸雄"}},
        {"alias": "お兄さん", "sender_in": {"西岡二記子", "西岡幸雄"}},
    ],
    "西岡はるな": [
        {"alias": "ママ"}, {"alias": "まま"}, {"alias": "まっま"}, {"alias": "ままん"}, {"alias": "まーまー"},
        {"alias": "はるな"}, {"alias": "はるなさん"},
    ],
    "西岡まひろ": [
        {"alias": "ピコ"}, {"alias": "ひぃちゃん"}, {"alias": "ぴこ"}, {"alias": "ぴこりん"},
        {"alias": "まひろさん"}, {"alias": "ひぃ"}, {"alias": "ひー"}, {"alias": "ひーちゃん"}, {"alias": "まひりん"},
    ],
    "西岡鷹春": [
        {"alias": "たか"}, {"alias": "ほっぺ"}, {"alias": "鷹春"}, {"alias": "鷹"},
        {"alias": "たかちゃん"}, {"alias": "鷹ちゃん"}, {"alias": "たかはさん"},
        {"alias": "たかすけ"}, {"alias": "たかのすけ"},
    ],
    "西岡二記子": [
        {"alias": "おばあちゃん", "recipient_in": {"西岡二記子"}},
        {"alias": "おばーちゃーん", "recipient_in": {"西岡二記子"}},
        {"alias": "三重の方のおばあちゃん"}, {"alias": "三重のおばあちゃん"},
    ],
    "西岡幸雄": [
        {"alias": "おじいちゃん", "recipient_in": {"西岡幸雄"}},
        {"alias": "おじーちゃーん", "recipient_in": {"西岡幸雄"}},
        {"alias": "三重の方のおじいちゃん"}, {"alias": "三重のおじいちゃん"},
    ],
    "井関いくこ": [
        {"alias": "おばあちゃん", "recipient_in": {"井関いくこ"}},
        {"alias": "おばーちゃーん", "recipient_in": {"井関いくこ"}},
        {"alias": "横浜の方のおばあちゃん"}, {"alias": "横浜のおばあちゃん"},
        {"alias": "お母さん", "sender_in": {"西岡はるな"}},
        {"alias": "おかあさん", "sender_in": {"西岡はるな"}},
    ],
    "井関正敏": [
        {"alias": "おじいちゃん", "recipient_in": {"井関正敏"}},
        {"alias": "おじーちゃーん", "recipient_in": {"井関正敏"}},
        {"alias": "横浜の方のおじいちゃん"}, {"alias": "横浜のおじいちゃん"},
        {"alias": "お父さん", "sender_in": {"西岡はるな"}},
        {"alias": "おとうさん", "sender_in": {"西岡はるな"}},
    ],
    "佐藤さとみ": [
        {"alias": "さとみちゃん"}, {"alias": "さとみ"},
        {"alias": "おねえちゃん", "sender_in": {"西岡はるな"}},
        {"alias": "お姉ちゃん", "sender_in": {"西岡はるな"}},
    ],
    "西岡洋介": [
        {"alias": "ようすけ"}, {"alias": "ようすけさん"}, {"alias": "洋介"},
        {"alias": "洋介さん"}, {"alias": "ようすけおじさん"}, {"alias": "洋介おじさん"},
    ],
    "西岡真希": [
        {"alias": "まきさん"},
    ],
}


def _rule_applies(rule, message):
    sender = str(message.get("sender", "")).strip()
    recipients = set(str(v).strip() for v in (message.get("recipient_list") or []) if str(v).strip())
    sender_in = rule.get("sender_in")
    recipient_in = rule.get("recipient_in")
    if sender_in and sender not in sender_in:
        return False
    if recipient_in and not recipients.intersection(recipient_in):
        return False
    return True


def participant_aliases_with_rules(name, message):
    aliases = participant_aliases(name)
    rules = NICKNAME_RULES.get(name, [])
    for rule in rules:
        if _rule_applies(rule, message):
            alias = str(rule.get("alias", "")).strip()
            if alias:
                aliases.add(alias)
                aliases.add(re.sub(r"\s+", "", alias))
    return {a for a in aliases if a}


def extract_when(text):
    s = text or ""
    patterns = [
        r"(今から|すぐに|至急|近日中|あとで)",
        r"(今日|明日|明後日|今週|来週|再来週)",
        r"(\d{1,2}月\d{1,2}日)",
        r"(\d{1,2}[/-]\d{1,2})",
        r"((?:月|火|水|木|金|土|日)曜日?)",
        r"(今週(?:月|火|水|木|金|土|日)曜日?)",
        r"(?:午前|午後)?\d{1,2}時(?:\d{1,2}分)?",
        r"(までに|締切|期限)",
    ]
    for p in patterns:
        m = re.search(p, s)
        if m:
            return m.group(0)
    return ""


def extract_where(text):
    s = text or ""
    patterns = [
        r"(駅の近くのコンビニ|駅の近く|コンビニ|スーパー|学校|会社|自宅|家|現地|オンライン)",
    ]
    for p in patterns:
        m = re.search(p, s)
        if m:
            return m.group(0)
    return ""


def extract_how(text):
    s = text or ""
    if re.search(r"(申し訳|謝罪)", s):
        return "申し訳なさそうに"
    if re.search(r"(丁寧|丁重)", s):
        return "丁寧に"
    return ""


def normalize_action_text(action, message):
    s = (action or "").strip()
    if not s:
        return "対応する"
    s = s.replace("ミスを治す", "ミスを直す")
    s = re.sub(r"[。！!？?]+$", "", s)
    s = re.sub(r"^(?:あとで|今から|すぐに|近日中に)\s*", "", s)
    # 呼びかけ除去（例: 西岡千尋、）
    s = re.sub(r"^[\wぁ-んァ-ン一-龥々ー]+(?:さん|くん|ちゃん)?[、,\s]+", "", s)

    sender = str(message.get("sender", "")).strip()
    sender_name = SELF_DISPLAY_NAME if _is_me_name(sender) else sender
    s = re.sub(r"(わたし|私|僕|ぼく|俺|おれ)(に|へ)", f"{sender_name}に", s)

    verb_map = [
        (r"してほしい$", "する"),
        (r"してください$", "する"),
        (r"してよ$", "する"),
        (r"して$", "する"),
        (r"やってよ$", "する"),
        (r"やって$", "する"),
        (r"送って$", "送る"),
        (r"確認して$", "確認する"),
        (r"買ってきて$", "買ってくる"),
        (r"謝罪して$", "謝罪する"),
    ]
    for pat, rep in verb_map:
        if re.search(pat, s):
            s = re.sub(pat, rep, s)
            break
    return s


def replace_first_person_with_sender(text, message):
    s = str(text or "")
    if not s:
        return s
    sender = str(message.get("sender", "")).strip()
    sender_name = SELF_DISPLAY_NAME if _is_me_name(sender) else sender
    if not sender_name:
        return s
    # 一人称を送信者名に置換（私立/私道などの語を避けるため後続文字を制限）
    patterns = [
        (r"(わたし|私|僕|ぼく|俺|おれ)(に|へ)", f"{sender_name}に"),
        (r"(わたし|私|僕|ぼく|俺|おれ)(は|が|も|を|の|から|まで)", f"{sender_name}\\2"),
        (r"(わたし|私|僕|ぼく|俺|おれ)(?=[、。！？!\s]|$)", sender_name),
        (r"送信者(は|が|も|を|の|から|まで)", f"{sender_name}\\1"),
        (r"話者(は|が|も|を|の|から|まで)", f"{sender_name}\\1"),
        (r"(送信者|話者)(?=[、。！？!\s]|$)", sender_name),
    ]
    out = s
    for pat, rep in patterns:
        out = re.sub(pat, rep, out)
    return out


def make_natural_task_sentence(action, text, message):
    base = normalize_action_text(action, message)
    when = extract_when(text)
    where = extract_where(text)
    how = extract_how(text)

    if not when and re.search(r"(して|やって|送って|確認して|買ってきて|してよ|してほしい|お願い|頼む)", text or ""):
        when = "今から"

    sentence = base
    tail = []
    if when and when not in sentence:
        tail.append(when)
    if where and where not in sentence:
        tail.append(f"{where}で")
    if how and how not in sentence:
        tail.append(how)

    if tail:
        mods = "".join(tail)
        if re.search(r"謝罪する$", sentence):
            sentence = re.sub(r"謝罪する$", f"謝罪を{mods}する", sentence)
        elif re.search(r"(する|送る|確認する|買ってくる)$", sentence):
            sentence = re.sub(r"(する|送る|確認する|買ってくる)$", lambda m: f"{mods}{m.group(1)}", sentence)
        else:
            sentence = f"{sentence}{mods}"
    if not sentence.endswith("。"):
        sentence += "。"
    return replace_first_person_with_sender(sentence, message)


def _is_me_name(name):
    return str(name or "").strip().lower() in {"me", "self", "自分"}


def find_explicit_targets(text, participant_names, message):
    explicit = []
    s = text or ""
    for p in participant_names:
        aliases = participant_aliases_with_rules(p, message)
        if any(a and a in s for a in aliases):
            explicit.append(p)
    return explicit


def infer_participant_tasks(message, tasks, features):
    participants = [str(p).strip() for p in (message.get("participants") or []) if str(p).strip()]
    recipient_list = [str(p).strip() for p in (message.get("recipient_list") or []) if str(p).strip()]
    direction = message.get("direction", "")
    text = message.get("content", "") or ""

    others = [p for p in participants if not _is_me_name(p)]
    result = {"me": []}
    for p in others:
        result.setdefault(p, [])

    if not tasks:
        return result

    explicit_targets = find_explicit_targets(text, others, message)

    trigger = (
        bool(features.get("request_expression"))
        or bool(features.get("confirmation_expression"))
        or bool(re.search(r"(締切|期限|提出|までに|して|してほしい|やって|送って|確認して|買ってきて|お願い|頼む)", text))
    )
    if not trigger:
        return result

    if explicit_targets:
        for target in explicit_targets:
            for t in tasks:
                result[target].append(make_natural_task_sentence(t, text, message))
        return result

    if direction == "OUTGOING":
        # 自分から相手への依頼は宛先側タスク。グループ時は過剰配布を避ける。
        preferred = [p for p in recipient_list if not _is_me_name(p)] or others
        # 明示名がないグループ依頼は「全宛先」に配布する。
        # ただし一人だけを示唆する語がある場合のみ先頭1名に絞る。
        if len(preferred) > 1 and re.search(r"(どちらか|誰か一人|1人だけ|一名のみ|代表して)", text):
            preferred = [preferred[0]]
        for target in preferred:
            for t in tasks:
                result.setdefault(target, [])
                result[target].append(make_natural_task_sentence(t, text, message))
    else:
        # 相手から来た依頼/期限通知は自分タスク
        for t in tasks:
            result["me"].append(make_natural_task_sentence(t, text, message))

    return result


def infer_task_owners(direction, text, features, tasks):
    if not tasks:
        return [], []
    s = text or ""
    request_or_action = (
        bool(features.get("request_expression"))
        or bool(features.get("confirmation_expression"))
        or bool(re.search(r"(締切|期限|提出|までに|対応|確認して|やって|頼む|お願い)", s))
    )
    if direction == "INCOMING":
        return (tasks if request_or_action else []), []
    if direction == "OUTGOING":
        return [], (tasks if request_or_action else [])
    return [], []


def fallback_analysis(message):
    content = message.get("content", "")
    direction = message.get("direction", "")
    emotion_label, emotion_score, nuance = sentiment_fallback(content)
    features = detect_language_features(content)
    content_for_display = replace_first_person_with_sender(content, message)
    topic = infer_topic(content_for_display)
    intent = infer_intent(content, features)
    tasks = infer_tasks(content_for_display)
    self_tasks, other_tasks = infer_task_owners(direction, content, features, tasks)
    participant_tasks = infer_participant_tasks(message, tasks, features)
    compact = re.sub(r"\s+", " ", content_for_display).strip()
    summary = compact[:48] + ("..." if len(compact) > 48 else "")
    return {
        "timing": {"minutes_since_previous": None, "topic_duration_minutes": None},
        "emotion": {"label": emotion_label, "score": round(float(emotion_score), 3), "nuance": nuance},
        "thread_metrics": {
            "messages_per_minute": None,
            "topics_per_minute": None,
            "messages_per_topic": None,
            "chars_per_message": None,
        },
        "language_features": features,
        "semantic": {
            "content_summary": summary if summary else "内容なし",
            "topic": topic,
            "intent": intent,
            "tasks": tasks,
            "self_tasks": self_tasks,
            "other_tasks": other_tasks,
            "participant_tasks": participant_tasks,
        },
    }


def sanitize_llm_analysis(obj, fallback, message):
    result = copy.deepcopy(fallback)
    if not isinstance(obj, dict):
        return result

    emotion = obj.get("emotion", {})
    if isinstance(emotion, dict):
        label = emotion.get("label")
        score = emotion.get("score")
        nuance = emotion.get("nuance")
        if isinstance(label, str) and label:
            result["emotion"]["label"] = label
        if isinstance(score, (int, float)):
            result["emotion"]["score"] = round(max(0.0, min(1.0, float(score))), 3)
        if isinstance(nuance, str) and nuance:
            result["emotion"]["nuance"] = nuance[:120]

    lf = obj.get("language_features", {})
    if isinstance(lf, dict):
        for key in result["language_features"].keys():
            if key in lf:
                if isinstance(result["language_features"][key], bool):
                    result["language_features"][key] = bool(lf[key])
                elif isinstance(lf[key], str):
                    result["language_features"][key] = lf[key][:80]

    semantic = obj.get("semantic", {})
    if isinstance(semantic, dict):
        for key in ("content_summary", "topic", "intent"):
            if isinstance(semantic.get(key), str) and semantic.get(key):
                value = semantic[key][:120]
                if key in {"content_summary", "topic"}:
                    value = replace_first_person_with_sender(value, message)
                result["semantic"][key] = value
        for task_key in ("tasks", "self_tasks", "other_tasks"):
            tasks = semantic.get(task_key)
            if isinstance(tasks, list):
                result["semantic"][task_key] = [
                    replace_first_person_with_sender(str(t)[:80], message)
                    for t in tasks if str(t).strip()
                ][:8]
        participant_tasks = semantic.get("participant_tasks")
        if isinstance(participant_tasks, dict):
            normalized = {}
            for name, arr in participant_tasks.items():
                key = "me" if _is_me_name(name) else str(name).strip()[:40]
                if not key:
                    continue
                if isinstance(arr, list):
                    normalized[key] = [
                        replace_first_person_with_sender(str(t)[:80], message)
                        for t in arr if str(t).strip()
                    ][:8]
            if normalized:
                result["semantic"]["participant_tasks"] = normalized

    # intent/summary の生文混入を抑える
    lf_now = result.get("language_features", {})
    result["semantic"]["intent"] = normalize_intent_label(
        result["semantic"].get("intent"),
        message.get("content", ""),
        lf_now,
    )
    result["semantic"]["content_summary"] = normalize_content_summary(
        result["semantic"].get("content_summary"),
        message,
        result["semantic"].get("topic"),
        result["semantic"].get("intent"),
    )

    # tasks の丸写し・非タスク化を抑える
    for k in ("tasks", "self_tasks", "other_tasks"):
        result["semantic"][k] = normalize_task_candidates(
            result["semantic"].get(k, []),
            message,
            lf_now,
        )

    all_tasks = []
    for task_key in ("tasks", "self_tasks", "other_tasks"):
        for item in result["semantic"].get(task_key, []):
            if item not in all_tasks:
                all_tasks.append(item)
    result["semantic"]["tasks"] = all_tasks[:8]

    if not result["semantic"].get("self_tasks") and not result["semantic"].get("other_tasks"):
        self_tasks, other_tasks = infer_task_owners(
            message.get("direction", ""),
            message.get("content", ""),
            result.get("language_features", {}),
            result["semantic"].get("tasks", []),
        )
        result["semantic"]["self_tasks"] = self_tasks
        result["semantic"]["other_tasks"] = other_tasks

    if not isinstance(result["semantic"].get("participant_tasks"), dict):
        result["semantic"]["participant_tasks"] = infer_participant_tasks(
            message,
            result["semantic"].get("tasks", []),
            result.get("language_features", {}),
        )
    else:
        # 明示宛先がある時は、誤って全員配布されたタスクを抑制
        pmap = result["semantic"]["participant_tasks"]
        others = [str(p).strip() for p in (message.get("participants") or []) if str(p).strip() and not _is_me_name(p)]
        explicit_targets = find_explicit_targets(message.get("content", ""), others, message)
        if explicit_targets and message.get("direction", "") == "OUTGOING":
            for name in list(pmap.keys()):
                if _is_me_name(name):
                    continue
                if name not in explicit_targets:
                    pmap[name] = []
            for target in explicit_targets:
                pmap.setdefault(target, [])

    # participant_tasks を正として互換フィールドを同期
    pmap = result["semantic"].get("participant_tasks", {}) or {}
    me_tasks = pmap.get("me", [])
    other_tasks_merged = []
    for name, arr in pmap.items():
        if _is_me_name(name):
            continue
        for t in arr:
            if t not in other_tasks_merged:
                other_tasks_merged.append(t)
    result["semantic"]["self_tasks"] = me_tasks[:8]
    result["semantic"]["other_tasks"] = other_tasks_merged[:8]
    merged = []
    for arr in [result["semantic"]["self_tasks"], result["semantic"]["other_tasks"]]:
        for t in arr:
            if t not in merged:
                merged.append(t)
    result["semantic"]["tasks"] = merged[:8]
    return result


def extract_json_blob(text):
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start:end + 1]


def should_use_heavy_model(message, analysis):
    if not OPENAI_MODEL_HEAVY or OPENAI_MODEL_HEAVY == OPENAI_MODEL_LIGHT:
        return False
    text = message.get("content", "") or ""
    lang = (analysis.get("language_features", {}) or {})
    semantic = (analysis.get("semantic", {}) or {})
    tasks = semantic.get("tasks", []) or []
    self_tasks = semantic.get("self_tasks", []) or []
    other_tasks = semantic.get("other_tasks", []) or []

    ambiguous_request = bool(lang.get("request_expression")) and not tasks
    owner_ambiguous = bool(tasks) and not self_tasks and not other_tasks
    multi_task = len(tasks) >= 2
    difficult_phrase = bool(re.search(r"(誰が|どっち|どちら|お願いできる|してもらえる|頼める|やっといて|対応お願い)", text))
    long_and_mixed = len(text) >= 70 and bool(lang.get("interrogative_expression")) and bool(lang.get("request_expression"))
    return ambiguous_request or owner_ambiguous or multi_task or difficult_phrase or long_and_mixed


def build_analysis_prompt(message, phase="light", first_pass=None):
    instruction = (
        "日本語チャット分析を行い、JSONのみを返してください。"
        "出力キーは emotion, language_features, semantic のみ。"
        "emotion: label(string), score(0-1), nuance(日本語1行)。"
        "language_features: typo_detected(bool), typo_note(string), wordplay_detected(bool), wordplay_note(string), "
        "question_expression(bool), interrogative_expression(bool), strong_assertion_expression(bool), "
        "request_expression(bool), assertive_expression(bool), speculative_expression(bool), "
        "impression_expression(bool), confirmation_expression(bool)。"
        "semantic: content_summary(日本語1行), topic(1-3語), intent(string), tasks(string配列), "
        "self_tasks(string配列), other_tasks(string配列)。"
        "tasks は全タスク、self_tasks は自分がやるタスク、other_tasks は相手がやるタスク。"
    )
    if phase == "heavy":
        instruction += (
            "主体判定と曖昧依頼の解消を優先し、self_tasks/other_tasks を厳密に分離してください。"
        )
    payload = {
        "instruction": instruction,
        "input": {
            "message_id": message["id"],
            "text": message.get("content", ""),
            "direction": message.get("direction", ""),
            "sender": message.get("sender", ""),
            "recipient": message.get("recipient", ""),
        },
    }
    if first_pass:
        payload["first_pass"] = first_pass
    return payload


def analyze_with_openai_model(message, model_name, phase="light", first_pass=None):
    if not OPENAI_ENABLED or not model_name:
        return None
    prompt = build_analysis_prompt(message, phase=phase, first_pass=first_pass)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    def try_chat_completions():
        request_body = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "あなたは日本語チャット分析器です。必ずJSONのみ返してください。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        data = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OPENAI_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        blob = extract_json_blob(content)
        if not blob:
            raise ValueError("No JSON in chat/completions response")
        return json.loads(blob)

    def try_responses_api():
        system_text = "あなたは日本語チャット分析器です。必ずJSONのみ返してください。"
        user_text = json.dumps(prompt, ensure_ascii=False)
        request_body = {
            "model": model_name,
            "input": f"{system_text}\n\n{user_text}",
        }
        data = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OPENAI_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        content = payload.get("output_text")
        if not content:
            try:
                parts = []
                for out in payload.get("output", []):
                    for c in out.get("content", []):
                        t = c.get("text")
                        if isinstance(t, str):
                            parts.append(t)
                content = "\n".join(parts)
            except Exception:
                content = ""
        blob = extract_json_blob(content or "")
        if not blob:
            raise ValueError("No JSON in responses API output")
        return json.loads(blob)

    for attempt in range(OPENAI_RETRIES + 1):
        try:
            return try_chat_completions()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            # gpt-5 系が chat/completions 非対応のケースは responses API へフォールバック
            if e.code == 400:
                try:
                    return try_responses_api()
                except urllib.error.HTTPError as e2:
                    body2 = ""
                    try:
                        body2 = e2.read().decode("utf-8", errors="ignore")
                    except Exception:
                        pass
                    if attempt >= OPENAI_RETRIES:
                        logging.warning(
                            f"OpenAI({model_name}) analysis failed for {message['id']} "
                            f"(chat 400 -> responses failed): HTTP {e2.code} / chat_body={body[:360]} / responses_body={body2[:360]}"
                        )
                        return None
                    continue
                except Exception as e2:
                    if attempt >= OPENAI_RETRIES:
                        logging.warning(
                            f"OpenAI({model_name}) analysis failed for {message['id']} "
                            f"(chat 400 -> responses failed): {e2} / chat_body={body[:360]}"
                        )
                        return None
                    continue
            if attempt >= OPENAI_RETRIES:
                logging.warning(f"OpenAI({model_name}) analysis failed for {message['id']}: HTTP {e.code} {body[:220]}")
                return None
        except Exception as e:
            if attempt >= OPENAI_RETRIES:
                logging.warning(f"OpenAI({model_name}) analysis failed for {message['id']}: {e}")
                return None
    return None


def enrich_with_thread_metrics(messages, analyses_by_id):
    thread_buckets = {}
    for msg in messages:
        thread_buckets.setdefault(msg["thread_id"], []).append(msg)

    thread_summaries = []

    for thread_id, items in thread_buckets.items():
        items.sort(key=lambda x: x["timestamp"])
        first_ts = parse_log_timestamp(items[0]["timestamp"])
        last_ts = parse_log_timestamp(items[-1]["timestamp"])
        duration_minutes = max((last_ts - first_ts).total_seconds() / 60.0, 1e-6)
        total_chars = sum(len((m.get("content") or "").replace("\n", "")) for m in items)

        topics = []
        for m in items:
            analysis = analyses_by_id.get(m["id"]) or fallback_analysis(m)
            topic = (analysis.get("semantic", {}) or {}).get("topic") or "不明"
            topics.append(topic)

        topic_count = 0
        prev_topic = None
        for topic in topics:
            if topic != prev_topic:
                topic_count += 1
                prev_topic = topic
        topic_count = max(topic_count, 1)

        messages_per_minute = len(items) / duration_minutes if duration_minutes > 0 else 0.0
        topics_per_minute = topic_count / duration_minutes if duration_minutes > 0 else 0.0
        messages_per_topic = len(items) / topic_count if topic_count > 0 else 0.0
        chars_per_message = total_chars / len(items) if items else 0.0

        prev_ts = None
        current_topic = None
        topic_start = None
        for idx, m in enumerate(items):
            ts = parse_log_timestamp(m["timestamp"])
            analysis = analyses_by_id.get(m["id"]) or fallback_analysis(m)
            analysis = copy.deepcopy(analysis)
            topic = (analysis.get("semantic", {}) or {}).get("topic") or "不明"

            if idx == 0:
                minutes_since_previous = 0.0
            else:
                minutes_since_previous = max((ts - prev_ts).total_seconds() / 60.0, 0.0)

            if current_topic != topic or topic_start is None:
                current_topic = topic
                topic_start = ts
            topic_duration = max((ts - topic_start).total_seconds() / 60.0, 0.0)

            analysis["timing"] = {
                "minutes_since_previous": round(minutes_since_previous, 1),
                "topic_duration_minutes": round(topic_duration, 1),
            }
            analysis["thread_metrics"] = {
                "messages_per_minute": round(messages_per_minute, 3),
                "topics_per_minute": round(topics_per_minute, 3),
                "messages_per_topic": round(messages_per_topic, 3),
                "chars_per_message": round(chars_per_message, 2),
            }
            analyses_by_id[m["id"]] = analysis
            prev_ts = ts

        participants = sorted(set(items[-1].get("participants", [])))
        thread_summaries.append(
            {
                "thread_id": thread_id,
                "participants": participants,
                "message_count": len(items),
                "topic_count": topic_count,
                "duration_minutes": round(duration_minutes, 1),
                "messages_per_minute": round(messages_per_minute, 3),
                "topics_per_minute": round(topics_per_minute, 3),
                "messages_per_topic": round(messages_per_topic, 3),
                "chars_per_message": round(chars_per_message, 2),
                "last_message_at": items[-1]["timestamp"],
                "latest_topic": topics[-1] if topics else "不明",
            }
        )

    thread_summaries.sort(key=lambda x: x["last_message_at"], reverse=True)
    return thread_summaries


def legacy_sentiment_from_analysis(analysis):
    label = (analysis.get("emotion", {}) or {}).get("label", "neutral")
    key = str(label).lower()
    if key in {"negative", "anxious"}:
        return {"emotion": "sad", "emoji": "😢", "label": "ネガティブ", "color": "#ff6b81"}
    if key in {"urgent"}:
        return {"emotion": "urgent", "emoji": "⏱️", "label": "緊急", "color": "#ffb74d"}
    if key in {"positive", "affectionate"}:
        return {"emotion": "happy", "emoji": "😊", "label": "ポジティブ", "color": "#34C759"}
    if key in {"mixed"}:
        return {"emotion": "mixed", "emoji": "🟨", "label": "ミックス", "color": "#ffd166"}
    return {"emotion": "neutral", "emoji": "😐", "label": "ニュートラル", "color": "#8E8E93"}


class AnalysisEngine:
    def __init__(self):
        self.q = queue.Queue()
        self.pending = set()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def enqueue(self, message):
        mid = message["id"]
        with ANALYSIS_LOCK:
            if mid in self.pending:
                return
            self.pending.add(mid)
        self.q.put(message)

    def _worker(self):
        while True:
            message = self.q.get()
            try:
                self._analyze_one(message)
            except Exception as e:
                logging.warning(f"Analysis worker failed for {message.get('id')}: {e}")
            finally:
                with ANALYSIS_LOCK:
                    self.pending.discard(message["id"])
                self.q.task_done()

    def _analyze_one(self, message):
        content_hash = hashlib.sha1((message.get("content", "")).encode("utf-8")).hexdigest()
        fallback = fallback_analysis(message)
        llm_light = analyze_with_openai_model(message, OPENAI_MODEL_LIGHT, phase="light")

        if llm_light:
            base = sanitize_llm_analysis(llm_light, fallback, message)
            source = f"openai:{OPENAI_MODEL_LIGHT}"
        else:
            base = fallback
            source = "fallback"

        if llm_light and should_use_heavy_model(message, base):
            llm_heavy = analyze_with_openai_model(
                message,
                OPENAI_MODEL_HEAVY,
                phase="heavy",
                first_pass=base,
            )
            if llm_heavy:
                base = sanitize_llm_analysis(llm_heavy, base, message)
                source = "openai:mixed" if OPENAI_MODEL_HEAVY != OPENAI_MODEL_LIGHT else f"openai:{OPENAI_MODEL_HEAVY}"

        with ANALYSIS_LOCK:
            entry = {
                "content_hash": content_hash,
                "base_analysis": base,
                "source": source,
                "updated_at": now_iso(),
            }
            ANALYSIS_STORE.setdefault("messages", {})
            ANALYSIS_STORE["messages"][message["id"]] = entry
            save_analysis_store(ANALYSIS_STORE)


ANALYZER = AnalysisEngine()


def parse_log_messages():
    if not os.path.exists(LOG_FILE):
        return []

    messages = []
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - (?:INFO|WARNING|ERROR) - \[(INCOMING|OUTGOING)\] (.+?) -> (.+?):\s?(.*)$"
    )
    generic_log_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - (?:INFO|WARNING|ERROR) - ")
    current = None

    def flush_current():
        nonlocal current
        if not current:
            return
        content_lines = list(current["content_lines"])
        while content_lines and content_lines[-1] == "":
            content_lines.pop()
        content = clean_message_content("\n".join(content_lines))
        recipient_values = parse_recipient_values(current["recipient_raw"])
        sender_display = resolve_contact(current["sender_raw"])
        recipient_display = ", ".join(resolve_contact(v) for v in recipient_values)

        participants = [sender_display] + [resolve_contact(v) for v in recipient_values]
        thread_tokens = sorted(set(canonical_identifier(v) for v in ([current["sender_raw"]] + recipient_values) if canonical_identifier(v)))
        thread_key = hashlib.sha1("|".join(thread_tokens).encode("utf-8")).hexdigest()[:12]

        msg_key = "|".join([
            current["timestamp"],
            current["direction"],
            current["sender_raw"],
            current["recipient_raw"],
            content,
        ])
        msg_id = hashlib.sha1(msg_key.encode("utf-8")).hexdigest()
        messages.append(
            {
                "id": msg_id,
                "timestamp": current["timestamp"],
                "direction": current["direction"],
                "sender": sender_display,
                "recipient": recipient_display,
                "participants": sorted(set(participants)),
                "recipient_list": [resolve_contact(v) for v in recipient_values],
                "thread_id": thread_key,
                "content": content,
            }
        )
        current = None

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw_line = line.rstrip("\n")
                match = pattern.match(raw_line)
                if match:
                    flush_current()
                    timestamp, direction, sender_raw, recipient_raw, content = match.groups()
                    current = {
                        "timestamp": timestamp,
                        "direction": direction,
                        "sender_raw": sender_raw,
                        "recipient_raw": recipient_raw,
                        "content_lines": [content],
                    }
                    continue

                if current and generic_log_pattern.match(raw_line):
                    flush_current()
                    continue
                if current is not None:
                    current["content_lines"].append(raw_line)
        flush_current()
    except Exception as e:
        logging.warning(f"Error reading log: {e}")

    # newest first
    messages.reverse()
    return messages[:100]


def build_messages_payload():
    messages = parse_log_messages()
    analyses_by_id = {}
    pending_count = 0
    to_enqueue = []

    with ANALYSIS_LOCK:
        store_messages = ANALYSIS_STORE.get("messages", {})
        for msg in messages:
            mid = msg["id"]
            content_hash = hashlib.sha1((msg.get("content", "")).encode("utf-8")).hexdigest()
            saved = store_messages.get(mid)
            if saved and saved.get("content_hash") == content_hash:
                analyses_by_id[mid] = copy.deepcopy(saved.get("base_analysis", {}))
            else:
                pending_count += 1
                to_enqueue.append(msg)
                analyses_by_id[mid] = fallback_analysis(msg)

    for msg in to_enqueue:
        ANALYZER.enqueue(msg)

    thread_summaries = enrich_with_thread_metrics(list(reversed(messages)), analyses_by_id)

    output_messages = []
    with ANALYSIS_LOCK:
        store_messages = ANALYSIS_STORE.get("messages", {})
        for msg in messages:
            mid = msg["id"]
            saved = store_messages.get(mid)
            status = "complete" if saved else "pending"
            source = saved.get("source") if saved else "fallback"
            output = copy.deepcopy(msg)
            output["analysis"] = analyses_by_id.get(mid, fallback_analysis(msg))
            output["analysis_status"] = status
            output["analysis_source"] = source
            output["sentiment"] = legacy_sentiment_from_analysis(output["analysis"])
            output_messages.append(output)

    return {
        "messages": output_messages,
        "thread_summaries": thread_summaries,
        "meta": {
            "analysis_provider": (f"openai:{OPENAI_MODEL_LIGHT}" if OPENAI_ENABLED else "fallback"),
            "openai_enabled": bool(OPENAI_ENABLED),
            "openai_model_light": OPENAI_MODEL_LIGHT,
            "openai_model_heavy": OPENAI_MODEL_HEAVY,
            "pending_count": pending_count,
            "updated_at": now_iso(),
        },
    }


def trigger_reanalysis(scope="all", thread_id=None):
    messages = parse_log_messages()
    target_ids = []
    with ANALYSIS_LOCK:
        if scope == "all":
            ANALYSIS_STORE["messages"] = {}
        elif scope == "thread" and thread_id:
            for msg in messages:
                if msg.get("thread_id") == thread_id:
                    ANALYSIS_STORE["messages"].pop(msg["id"], None)
        save_analysis_store(ANALYSIS_STORE)

    for msg in messages:
        if scope == "thread" and thread_id and msg.get("thread_id") != thread_id:
            continue
        target_ids.append(msg["id"])
        ANALYZER.enqueue(msg)
    return len(target_ids)


class ViewerHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/reanalyze":
            self.send_error(404, "Not Found")
            return
        body = {}
        try:
            content_len = int(self.headers.get("Content-Length", "0"))
            if content_len > 0:
                body = json.loads(self.rfile.read(content_len).decode("utf-8"))
        except Exception:
            body = {}

        scope = body.get("scope", "all")
        thread_id = body.get("thread_id")
        queued = trigger_reanalysis(scope=scope, thread_id=thread_id)
        payload = json.dumps({"ok": 1, "queued": queued}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/messages":
            data = build_messages_payload()
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/":
            self.path = "/index.html"

        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer")
        req_path = self.path.split("?")[0].lstrip("/")
        requested_path = os.path.abspath(os.path.join(base_dir, req_path))
        if not requested_path.startswith(base_dir):
            self.send_error(403, "Forbidden")
            return

        if os.path.exists(requested_path) and os.path.isfile(requested_path):
            try:
                with open(requested_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                if requested_path.endswith(".html"):
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                elif requested_path.endswith(".css"):
                    self.send_header("Content-Type", "text/css; charset=utf-8")
                elif requested_path.endswith(".js"):
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, f"Server Error: {e}")
        else:
            self.send_error(404, "Not Found")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass


if __name__ == "__main__":
    port = 8080
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, ViewerHandler)
    print(f"Server running on http://localhost:{port}")
    print("Serving UI at http://localhost:8080")
    print(
        "Analysis provider: "
        + (f"openai(light={OPENAI_MODEL_LIGHT}, heavy={OPENAI_MODEL_HEAVY})" if OPENAI_ENABLED else "fallback")
    )
    httpd.serve_forever()
