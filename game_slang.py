"""Small, contextual glossary hints; never replacements or language detection.

Call after masking linked items/URLs/acronyms. Research candidates and sources
live in game_slang_research.json; only the selected subset below is active.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Hint:
    pattern: re.Pattern
    meaning: str
    context: re.Pattern | None = None
    exclude: re.Pattern | None = None


def _hint(pattern, meaning, context=None, *, exclude=None):
    return Hint(re.compile(pattern, re.IGNORECASE), meaning,
                re.compile(context, re.IGNORECASE) if context else None,
                re.compile(exclude, re.IGNORECASE) if exclude else None)


def _words(forms):
    return r"(?<!\w)(?:" + forms + r")(?!\w)"


_RU_COMBAT = r"\b(?:моб\w*|хил\w*|ресни\w*|кач\w*|агр\w*|пати|танк\w*)\b"
_ZH_COMBAT = r"(?:怪物|拉怪|引怪|刷怪|抢怪|搶怪|奶妈|奶媽|补师|補師|组队|組隊|回城|坦克|练级|練級)"

# Incoming non-English chat. Existing v1.3.5 hints retain their wording.
_INCOMING = (
    # Keep the common plural address distinct from a single person or name.
    _hint(r"\A\s*(?a:kal[ih]spera(?:[ \t]+|[ \t]*,[ \t]*)paidia)[ \t.!?…]*\Z",
          'Greeklish paidia (παιδιά) in a greeting = guys/children (plural)'),
    # Short Greeklish greetings; keep quoted names and longer prose contextual.
    _hint(r"\A\s*(?a:kal[ih]spera(?:(?:[ \t]+|[ \t]*,[ \t]*)[A-Za-z][A-Za-z0-9_-]{1,39})?)[ \t.!?…]*\Z",
          'Greeklish kalhspera/kalispera (καλησπέρα) = good evening'),
    # Standalone approval; do not reinterpret a literal place or a question.
    _hint(r"\A[\s¡!]*bien\s+ah[ií](?:[\s!.…]|[:;]-?[)D])*\Z",
          'Spanish bien ahí / bien ahi = Nice!'),
    # Complete Greeklish say-so clauses: keep grammatical person distinct.
    _hint(r"(?<!\w)afou\s+to\s+lete\s+kai\s+oi\s+d(?:yo|io)(?=[.!?…\s]*\Z)",
          'Greeklish afou to lete kai oi dyo/dio (αφού το λέτε και οι δυο) = since both of you say so; kala (καλά) = okay/well'),
    _hint(r"(?<!\w)afou\s+to\s+lene\s+kai\s+oi\s+d(?:yo|io)(?=[.!?…\s]*\Z)",
          'Greeklish afou to lene kai oi dyo/dio (αφού το λένε και οι δυο) = since both of them say so; kala (καλά) = okay/well'),
    # A promotional ending, not a quoted title or a phrase followed by other chat.
    _hint(r"(?<!\w)s[oó]\s+vem[\s,;:!.-]+o\s+pai\s+t[aá]\s+on(?=[.!?…\s]*\Z)",
          "In a Brazilian Portuguese streaming invitation, so vem o pai ta on (or só vem o pai tá on) means come join me, I'm live. Translate the invitation, and keep the whole channel title including its language tag unchanged",
          r"(?<!\w)(?:live|stream(?:ing)?|youtube|twitch)(?!\w)"),
    _hint(r"收[個个把]\s*(?:普[頂顶]\s*)?(?:(?:可(?:以)?|能)吃蛋的)?\s*(?:法杖|杖|弓|武器)(?![\u3400-\u9fff])",
          "收個/收个/收把 equipment = looking to buy that equipment in a trade listing"),
    _hint(r"普[頂顶]", "普頂/普顶 = top no-grade equipment", r"(?:法杖|杖|弓|武器)"),
    _hint(r"吃蛋", "吃蛋 = use shots (game consumables), without assuming a shot subtype",
          r"(?:(?:可以|可|能不能|不能|能否|能)吃蛋的?(?:法杖|杖|武器|弓)|(?:法杖|杖|武器|弓)(?:可以|可|能不能|不能|能否|能)吃蛋)"),
    _hint(r"\bno\s+precinho\b", "no precinho = at a good price (Portuguese price slang)",
          r"\[\[_*L2ITEM\d+\]\]|\b(?:vendo|vendendo|vender|compro|comprar|comprando|troco|oferta|ofertas)\b|(?<!\w)\d+(?:[.,]\d+)*k{1,2}(?!\w)"),
    _hint(_words("куплю|купим|купить|покупаю|покупаем"), "куплю = buying"),
    _hint(_words("продам|продадим|продать|продаю|продаем|продаём"), "продам = selling"),
    _hint(_words(r"сос(?:ки|ок|ками|ках|ку)"), "соски = shots (game ammunition, not body parts)"),
    _hint(r"\bБ[ -]+грейд(?:а|ом|у)?\b", "Б грейда = B-grade"),
    _hint(_words(r"спойл(?:а|у|ом|ер|ера|еру|ером|еры|еров)?"),
          "спойл = Spoil skill; спойлер = spoiler character; recruiting спойла means recruiting a spoiler"),
    _hint(_words("ШЕ"), "ШЕ = SE (Shillien Elder)"),
    _hint(_words("кач"), "кач = leveling"),
    _hint(_words("каты|катах"), "каты/катах = catacombs"),
    _hint(_words("парик|парики|парика|паровоз"), "парик/паровоз = mob train, a group of following monsters", _RU_COMBAT),
    _hint(_words("ресни|ресните|реснуть"), "ресни/ресните = resurrect/revive a dead character"),
    _hint(_words(r"хил(?:а|ер|ера|ер[ау]м)?"), "хил/хила = healer in recruitment or resurrection; хилить = heal", _RU_COMBAT + r"|\b(?:ищу|ищем|нужен)\b"),
    _hint(_words("КП|конста|консту"), "КП/конста = constant party, a regular organized group",
          r"\b(?:ищем|ищу|прайм|саппорт\w*|набор)\b"),
    _hint(_words("прайм"), "прайм = scheduled group play time", r"\b(?:КП|конст\w*|пати)\b"),
    _hint(r"\b(?:ща|щас)\s+приду\b", "ща приду = I will be there soon"),
    _hint(_words("агри|агрить|агрите"), "агри/агрить = aggro/provoke mobs"),
    _hint(_words("kuplu|kuplyu|kupliu"), "kuplu = I want to buy"),
    _hint(_words("soski"), "soski = shots (game ammunition)", r"\b(?:kuplu|kuplyu|prodam|greid\w*|shot\w*)\b"),
    _hint(r"\bB[ -]+(?:greid|greyd)(?:a)?\b", "B greida = B-grade"),
    _hint(_words("hil|khil"), "hil/khil = healer in recruitment", r"\b(?:nuzhen|nujen|ishu|ishchu|pati)\b"),
    _hint(_words("pati|katy"), "pati = party; katy = catacombs (Russian transliteration)",
          r"\b(?:nuzhen|nujen|ishu|ishchu|hil|khil)\b"),
    _hint(_words("upar|upando|upei|upou|upamos"), "upar = level up"),
    _hint(r"\bme\s+buf(?:f)?a\b", "me bufa = buff me"),
    _hint(_words("ressar|ressa"), "ressar = resurrect; morri = I died", r"\bmorri\b"),
    _hint(_words("pj|pjs"), "pj = player character"),
    _hint(r"(?:奶妈|奶媽|补师|補師)", "奶妈/奶媽/补师/補師 = healer"),
    _hint(r"打手", "打手 = damage dealer, not tank", _ZH_COMBAT),
    _hint(r"(?:拉怪|引怪)", "拉怪/引怪 = pull/lure mobs"),
    _hint(r"(?:没蓝|沒藍|没魔|沒魔)", "没蓝/沒藍 = out of mana", _ZH_COMBAT),
    _hint(r"(?:开火车|開火車)", "开火车/開火車 = bring a mob train", _ZH_COMBAT),
    # One rule for both names avoids a shorter alias overlapping a blessed name.
    _hint(r"(?:魔灵弹|魔靈彈)", "魔灵弹/魔靈彈 = Spiritshot; 祝福(的)魔灵弹/祝福(的)魔靈彈 = Blessed Spiritshot"),
    _hint(r"(?:灵魂弹|靈魂彈|灵魂蛋|靈魂蛋)", "灵魂弹/靈魂彈/靈魂蛋 = Soulshot"),
    _hint(r"(?:收购|收購|求购|求購)", "收购/收購/求购/求購 = buying"),
    _hint(r"(?:私聊|私讯|私訊|密我)", "私聊/私訊/密我 = private message / whisper"),
    # A time qualifier; do not force the abbreviation's city/name uses.
    _hint(r"(?<![\w:])\d{1,2}(?::\d{2})?(?:\s*[-–]\s*\d{1,2}(?::\d{2})?)?\s*мск(?!\w)",
          "мск = Moscow time (MSK)"),
)


# A Spanish reply gets only the narrow English gaming vocabulary below.
# Real agriculture and noun/proper-name uses are deliberately excluded.
_EN_LITERAL_FARMING = (r"(?<!\w)(?:agricultur\w*|crops?|wheat|corn|maize|rice|potatoes?|tomatoes?|"
                       r"vegetables?|tractors?|livestock|cattle|dairy|barns?|pastures?|orchards?|harvest\w*)(?!\w)"
                       r"|\bfarming\s+(?:is|was|industry|business|occupation|profession)\b"
                       r"|\b(?:work|job|occupation|profession)\s+(?:in|is|was)\s+farming\b")
_EN_FARMING_ACTIVITY = (r"\A\s*farming\b"
                        r"|\b(?:am|is|are|was|were|be|been|not|keep|stop|start|started|stopped)\s+farming\b"
                        r"|(?<!\w)(?:I['’]m|we['’]re|you['’]re|they['’]re)\s+farming\b")
_EN_FARM_VERB = (r"(?<!\w)(?:I|we|you|they)\s+(?:(?:will|can|could|should|must|would|might|may)\s+(?:not\s+)?|(?:do|did)\s+not\s+|don['’]t\s+)?farm(?!\w)"
                 r"|(?<!\w)(?:I|we|you|they)\s+(?:want|need|plan|like|love|hate|prefer)\s+to\s+farm(?!\w)"
                 r"|(?<!\w)(?:let['’]s\s+(?:not\s+)?|(?:can|could|should)\s+(?:I|we|you|they)\s+)farm(?!\w)")
_OUTGOING_SPANISH = (
    _hint(_words("farming"),
          'farming (gaming) = farmeando',
          _EN_FARMING_ACTIVITY, exclude=_EN_LITERAL_FARMING),
    _hint(_words("farm"), "farm (gaming) = farmear", _EN_FARM_VERB,
          exclude=_EN_LITERAL_FARMING),
)

MAX_HINTS = 8
MAX_HINT_CHARS = 1000


def game_glossary(text: str, target_language: str) -> str:
    """Bound context size and select relevant hints without modifying text."""
    target = target_language.casefold()
    if target == "english":
        rules = _INCOMING
    elif target == "spanish":
        rules = _OUTGOING_SPANISH
    else:
        return ""
    meanings = []
    length = 0
    for rule in rules:
        if rule.exclude and rule.exclude.search(text):
            continue
        if not rule.pattern.search(text) or (rule.context and not rule.context.search(text)):
            continue
        if len(meanings) >= MAX_HINTS or length + len(rule.meaning) + 2 > MAX_HINT_CHARS:
            break
        meanings.append(rule.meaning)
        length += len(rule.meaning) + 2
    if not meanings:
        return ""
    return (" Lineage II vocabulary for this message: " + "; ".join(meanings)
            + ". Use these game meanings when appropriate. Preserve buying versus selling and negation.")
