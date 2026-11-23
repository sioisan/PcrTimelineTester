"""角色数据：从摸轴器的 ``StreamingAssets\\Datas`` 读角色名，管理角色图标缓存。

摸轴器安装目录下有个 ``*_Data\\StreamingAssets\\Datas`` 文件夹，里面的几个 JSON
是游戏里的原始资料表（未混淆、UTF-8）：

* ``chara_name.json``：4 位「角色号」→ 多语言名字数组，第一项是中文名；
* ``UnitNickNameDic.json``：6 位 ``unitId`` → 国服常用简称（``115001 => 春花``）；
* ``nickname.json``：6 位 ``unitId`` → 中文名（``115001 => 新年似似花``）；
* ``GuildEnemyDatas.json``：公会战 Boss 的 id 分组。

6 位 ``unitId`` 的前四位就是角色号（``115001 // 100 == 1150``），所以全名要靠
``chara_name[str(unitId // 100)][0]`` 查。

图标走第三方镜像站 ``https://wthee.xyz/redive/jp/resource/icon/unit/{角色号}{两位后缀}.webp``，
文件名是「4 位角色号 + 11/31/61」（``115001`` 对应 ``115011.webp``）。只认 ``unitId < 190000``
的角色，缓存放在 ``%LOCALAPPDATA%\\PcrTimelineTester\\icons``，文件名就是那串 6 位编码，
所以 ``icon.zip`` 可以直接解压进去当默认图标包用。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import zipfile

import httpx

from .settings import app_dir

ICON_URL = "https://wthee.xyz/redive/jp/resource/icon/unit/{code}.webp"
ICON_SUFFIXES = ("61", "31", "11")  # 优先用最大的那套立绘
ICON_DIR_NAME = "icons"
ICON_HANDLE = ".webp"
DATAS_DIR_NAME = "StreamingAssets"
DATAS_SUBDIR = "Datas"
DOWNLOAD_TIMEOUT = 20.0
UNIT_LIMIT = 190_000
DEFAULT_WORKERS = 8
UNIT_INFO_URL = "http://192.144.214.135/pcr/unit_info.php"
UNIT_INFO_FILE = "unit_info.json"
UNIT_INFO_TIMEOUT = 30.0
ROLE_LABELS: dict[int, str] = {
    1: "攻击型",
    2: "破防型",
    3: "增益型",
    4: "减益型",
    5: "强化型",
    6: "治疗型",
    7: "坦克型",
    8: "干扰型",
}
TALENT_LABELS: dict[int, str] = {1: "火", 2: "水", 3: "风", 4: "光", 5: "暗"}


def role_label(role: int) -> str:
    return ROLE_LABELS.get(int(role or 0), "")


def talent_label(talent: int) -> str:
    return TALENT_LABELS.get(int(talent or 0), "")


@dataclass(frozen=True, slots=True)
class UnitInfo:
    """``unit_info.php`` 里的一条：职能 / 属性 / 攻击范围（键是 6 位 unitId）。"""

    role: int = 0      # 职能（1 攻击型 … 8 干扰型）
    talent: int = 0    # 属性（1 火 … 5 暗）
    width: int = 0     # searchAreaWidth，越大越靠后


def unit_info_path() -> Path:
    return app_dir() / UNIT_INFO_FILE


def _info_from_payload(raw) -> dict[int, UnitInfo]:
    result: dict[int, UnitInfo] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        unit = _as_int(key)
        if unit is None or not 0 < unit < UNIT_LIMIT or not isinstance(value, dict):
            continue
        role = _as_int(value.get("role")) or 0
        talent = _as_int(value.get("talent")) or 0
        width = _as_int(value.get("search_area_width")) or 0
        if not (role or talent or width):
            continue
        result[unit] = UnitInfo(role, talent, width)
    return result


def _parse_unit_info(text: str) -> dict[int, UnitInfo]:
    """``unit_info.php`` 直接吐一个 JSON 对象；顺手兼容被 JS 包一层的写法。"""
    body = (text or "").strip()
    if not body:
        return {}
    try:
        return _info_from_payload(json.loads(body))
    except ValueError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return _info_from_payload(json.loads(body[start : end + 1]))
    except ValueError:
        return {}


def _load_unit_info_file() -> dict[int, UnitInfo]:
    return _info_from_payload(_load_json(unit_info_path(), {}))


def load_unit_info(
    *, refresh: bool = False, timeout: float = UNIT_INFO_TIMEOUT, log=print
) -> dict[int, UnitInfo]:
    """角色资料表：先读本地缓存，缺了（或 refresh）就去 ``unit_info.php`` 抓一次。"""
    cached = _load_unit_info_file()
    if cached and not refresh:
        install_unit_info(cached)
        return cached
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(UNIT_INFO_URL)
            response.raise_for_status()
            fetched = _parse_unit_info(response.text)
    except (httpx.HTTPError, OSError) as exc:
        log(f"提示：取不到角色资料表（{type(exc).__name__}：{exc}），先用本地缓存")
        install_unit_info(cached)
        return cached
    if not fetched:
        log("提示：unit_info.php 里的内容解析不出来，先用本地缓存")
        install_unit_info(cached)
        return cached
    try:
        target = unit_info_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            str(unit): {
                "role": entry.role,
                "talent": entry.talent,
                "search_area_width": entry.width,
            }
            for unit, entry in sorted(fetched.items())
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError as exc:
        log(f"提示：角色资料表写不进缓存（{exc}）")
    install_unit_info(fetched)
    return fetched


def icon_dir() -> Path:
    return app_dir() / ICON_DIR_NAME


def icon_codes(unit_id: int) -> list[str]:
    """这个 unitId 可能的图标编码（``115001 -> ["115011", "115031", "115061"]``）。"""
    unit = int(unit_id)
    if unit <= 0 or unit >= UNIT_LIMIT:
        return []
    base = unit // 100
    return [f"{base}{suffix}" for suffix in ICON_SUFFIXES]


def cached_icon(unit_id: int) -> Path | None:
    """本地已有的图标文件（不联网）。"""
    for code in icon_codes(unit_id):
        path = icon_dir() / f"{code}{ICON_HANDLE}"
        if path.is_file():
            return path
    return None


def locate_datas(target: Path) -> Path | None:
    """在 PCRCalculator 安装目录里找 ``...\\<X>_Data\\StreamingAssets\\Datas``。"""
    target = Path(target)
    candidates: list[Path] = []
    if target.is_dir():
        direct = target / DATAS_DIR_NAME / DATAS_SUBDIR
        if (direct / "chara_name.json").is_file():
            candidates.append(direct)
        candidates.extend(sorted(target.glob(f"*_Data/{DATAS_DIR_NAME}/{DATAS_SUBDIR}")))
        if target.name == DATAS_SUBDIR:
            candidates.insert(0, target)
    for candidate in candidates:
        if (candidate / "chara_name.json").is_file():
            return candidate
    return None


def _load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


_info_table: dict[int, UnitInfo] | None = None


def install_unit_info(table: dict[int, UnitInfo]) -> None:
    """把已经读到的资料表装进内存（之后 ``unit_width()`` 不必再读文件）。"""
    global _info_table
    _info_table = dict(table)


def _unit_info() -> dict[int, UnitInfo]:
    """内存里的资料表；没有就读一次缓存文件（**不联网**，联网是 ``load_unit_info`` 的事）。"""
    global _info_table
    if _info_table is None:
        _info_table = _load_unit_info_file()
    return _info_table


def _info_for(info: dict[int, UnitInfo], unit_id: int) -> UnitInfo | None:
    """按 unitId 查；表里万一是 4 位角色号，也认一下。"""
    unit = int(unit_id)
    return info.get(unit) or info.get(unit // 100)


def unit_talent(unit_id: int) -> int | None:
    entry = _info_for(_unit_info(), unit_id)
    return entry.talent if entry else None


def unit_width(unit_id: int) -> int | None:
    """角色的攻击范围（越大越靠后）。战斗里的站位就是这个值从小到大排出来的。"""
    entry = _info_for(_unit_info(), unit_id)
    if entry is None or entry.width <= 0:
        return None
    return int(entry.width)


@dataclass(frozen=True, slots=True)
class Character:
    unit_id: int
    name: str          # chara_name 里的中文全名
    nickname: str      # 常见简称（UnitNickNameDic 优先，其次 nickname.json）
    base_id: int       # unitId // 100
    role: int = 0      # 职能（1 攻击型 … 8 干扰型）
    talent: int = 0    # 属性（1 火 … 5 暗）

    @property
    def label(self) -> str:
        return self.nickname or self.name or f"角色{self.base_id}"

    @property
    def role_label(self) -> str:
        return role_label(self.role)

    @property
    def talent_label(self) -> str:
        return talent_label(self.talent)

    @property
    def group_label(self) -> str:
        parts = [part for part in (self.role_label, self.talent_label) if part]
        return "·".join(parts)


class DataBook:
    """一个摸轴器安装目录里的角色资料（名字 / 图标）。"""

    def __init__(self, datas_dir: Path, info: dict[int, UnitInfo] | None = None):
        self.datas_dir = Path(datas_dir)
        self.info: dict[int, UnitInfo] = dict(info or {})
        self._names: dict[int, str] = {}
        self._nicknames: dict[int, str] = {}
        self._enemies: list[int] = []
        self._characters: list[Character] | None = None
        self._load()

    # ---------------------------------------------------------------- 读取
    def _load(self) -> None:
        raw_names = _load_json(self.datas_dir / "chara_name.json", {})
        if isinstance(raw_names, dict):
            for key, value in raw_names.items():
                names = value if isinstance(value, list) else [value]
                text = next((str(item) for item in names if isinstance(item, str) and item.strip()), "")
                base = _as_int(key)
                if base is not None and text:
                    self._names[base] = text
        nicknames = _load_json(self.datas_dir / "UnitNickNameDic.json", {})
        if isinstance(nicknames, dict):
            for key, value in nicknames.items():
                unit = _as_int(key)
                if unit is not None and isinstance(value, str) and value.strip():
                    self._nicknames[unit] = value.strip()
        plain = _load_json(self.datas_dir / "nickname.json", {})
        if isinstance(plain, dict):
            for key, value in plain.items():
                unit = _as_int(key)
                if unit is not None and isinstance(value, str) and value.strip():
                    self._nicknames.setdefault(unit, value.strip())
        enemies = _load_json(self.datas_dir / "GuildEnemyDatas.json", {})
        if isinstance(enemies, dict):
            for group in enemies.get("enemyIds") or []:
                if isinstance(group, list):
                    self._enemies.extend(int(item) for item in group if _as_int(item) is not None)

    # ---------------------------------------------------------------- 查询
    def name(self, unit_id: int) -> str:
        base = int(unit_id) // 100
        return self._names.get(base) or f"角色{base}"

    def nickname(self, unit_id: int) -> str:
        return self._nicknames.get(int(unit_id), "")

    def label(self, unit_id: int) -> str:
        return self.nickname(unit_id) or self.name(unit_id)

    @property
    def enemies(self) -> list[int]:
        return list(self._enemies)

    def characters(self) -> list[Character]:
        """所有能查到的 6 位 unitId（按角色号排序，只保留 19 万以下且有职能 / 属性的）。"""
        if self._characters is None:
            ids = {
                unit
                for unit in self._nicknames
                if 0 < unit < UNIT_LIMIT and self.role_of(unit) and self.talent_of(unit)
            }
            for unit in ids.copy():
                # 只有简称、连全名都查不到的角色留着也没用（显示成「角色1099」那种）
                if not self.nickname(unit) and self.name(unit) == f"角色{unit // 100}":
                    ids.discard(unit)
            self._characters = [
                Character(
                    unit,
                    self.name(unit),
                    self.nickname(unit),
                    unit // 100,
                    self.role_of(unit),
                    self.talent_of(unit),
                )
                for unit in sorted(ids)
            ]
        return list(self._characters)

    def units_without_info(self) -> list[int]:
        """Datas 里有名字、但资料表里查不到职能或属性的角色（用来决定要不要重抓）。"""
        missing: list[int] = []
        for unit in sorted(self._nicknames):
            if not 0 < unit < UNIT_LIMIT:
                continue
            if not self.nickname(unit) and self.name(unit) == f"角色{unit // 100}":
                continue  # 连名字都没有的角色不算（Datas 里迟早会有）
            if not (self.role_of(unit) and self.talent_of(unit)):
                missing.append(unit)
        return missing

    # ---------------------------------------------- 职能 / 属性 / 站位宽度
    def info_of(self, unit_id: int) -> UnitInfo | None:
        return _info_for(self.info, unit_id)

    def role_of(self, unit_id: int) -> int:
        entry = self.info_of(unit_id)
        return int(entry.role) if entry else 0

    def talent_of(self, unit_id: int) -> int:
        entry = self.info_of(unit_id)
        return int(entry.talent) if entry else 0

    def width_of(self, unit_id: int) -> int | None:
        entry = self.info_of(unit_id)
        return int(entry.width) if entry and entry.width > 0 else None

    # ---------------------------------------------------------------- 图标
    def icon_path(self, unit_id: int, *, download: bool = True) -> Path | None:
        """本地缓存的图标文件；没有且允许下载时就下载一次（文件名是图标编码）。"""
        cached = cached_icon(unit_id)
        if cached is not None:
            return cached
        if not download:
            return None
        codes = icon_codes(unit_id)
        if not codes:
            return None
        icon_dir().mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                for code in codes:
                    reply = client.get(ICON_URL.format(code=code))
                    if reply.status_code != 200 or not reply.content:
                        continue
                    path = icon_dir() / f"{code}{ICON_HANDLE}"
                    temporary = path.with_name(path.name + ".part")
                    temporary.write_bytes(reply.content)
                    os.replace(temporary, path)
                    return path
        except (httpx.HTTPError, OSError):
            return None
        return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def open_book(target: Path | None, *, info: dict[int, UnitInfo] | None = None) -> DataBook | None:
    """``target`` 可以是摸轴器目录、``*_Data`` 或 ``Datas``；找不到就返回 None。"""
    if target is None:
        return None
    datas = locate_datas(Path(target))
    if datas is None:
        return None
    table = dict(info) if info is not None else _unit_info()  # 只读缓存，联网是 load_unit_info 的事
    return DataBook(datas, info=table)


# -------------------------------------------------------------------- 批量下载
def download_icons(
    unit_ids,
    *,
    workers: int = DEFAULT_WORKERS,
    progress=None,
    log=print,
    cancel=None,
) -> tuple[int, int, int]:
    """多线程下载一批角色的图标。

    ``progress(done, total, unit_id)`` 在每个角色处理完后调用（主线程回调），
    ``cancel()`` 返回真时尽快停下。返回 ``(下载成功, 已有跳过, 失败)``。
    """
    wanted: list[int] = []
    seen: set[int] = set()
    for unit in unit_ids:
        try:
            value = int(unit)
        except (TypeError, ValueError):
            continue
        if value in seen or not icon_codes(value):
            continue
        seen.add(value)
        if cached_icon(value) is None:
            wanted.append(value)
    total = len(wanted)
    skipped = len(seen) - total
    if not wanted:
        if progress is not None:
            progress(0, 0, 0)
        log(f"图标：{skipped} 个已经在缓存里，没有需要下载的。")
        return 0, skipped, 0

    log(f"图标：准备下载 {total} 个（{workers} 线程），缓存目录 {icon_dir()}")
    icon_dir().mkdir(parents=True, exist_ok=True)
    downloaded = 0
    failed = 0
    done = 0

    def fetch(unit: int) -> bool:
        book = _client_pool.get()
        try:
            for code in icon_codes(unit):
                reply = book.get(ICON_URL.format(code=code))
                if reply.status_code != 200 or not reply.content:
                    continue
                path = icon_dir() / f"{code}{ICON_HANDLE}"
                temporary = path.with_name(path.name + f".{os.getpid()}.part")
                temporary.write_bytes(reply.content)
                os.replace(temporary, path)
                return True
        except (httpx.HTTPError, OSError):
            return False
        return False

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {pool.submit(fetch, unit): unit for unit in wanted}
        for future, unit in futures.items():
            if cancel is not None and cancel():
                break
            ok = False
            try:
                ok = future.result()
            except Exception:  # noqa: BLE001 - 单个失败不该影响整体
                ok = False
            done += 1
            if ok:
                downloaded += 1
            else:
                failed += 1
            if progress is not None:
                progress(done, total, unit)
    if cancel is not None and cancel():
        log(f"图标：已取消（完成 {done}/{total}）。")
    log(f"图标：完成 {downloaded} 个，已有 {skipped} 个，失败 {failed} 个。")
    return downloaded, skipped, failed


class _ClientPool:
    """每个线程一个 httpx 客户端（``threading.local``）。"""

    def __init__(self) -> None:
        import threading

        self._local = threading.local()

    def get(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
            self._local.client = client
        return client


_client_pool = _ClientPool()


# -------------------------------------------------------------------- 图标包
def import_icon_pack(zip_path: Path, *, progress=None, log=print) -> int:
    """把 ``icon.zip`` 里的 ``*.webp`` 解压进图标缓存目录，返回解压数量。"""
    zip_path = Path(zip_path)
    target = icon_dir()
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as archive:
        members = [item for item in archive.infolist() if item.filename.lower().endswith(ICON_HANDLE)]
        total = len(members)
        if total == 0:
            log("图标包：这个压缩包里没有 .webp 文件。")
            return 0
        for index, item in enumerate(members, start=1):
            name = Path(item.filename.replace("\\", "/")).name
            if not name or name.startswith("."):
                continue
            destination = target / name
            if destination.is_file():
                continue
            with archive.open(item) as source, destination.open("wb") as sink:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    sink.write(chunk)
            count += 1
            if progress is not None:
                progress(index, total, name)
    log(f"图标包：解压 {count} 个图标到 {target}")
    return count



# -------------------------------------------------------------------- 图标确认
def find_icon_pack(*roots: Path) -> Path | None:
    """在几个候选目录里找 ``icon.zip``（发布包和源码目录都放同一份）。"""
    for root in roots:
        if root is None:
            continue
        candidate = Path(root) / "icon.zip"
        if candidate.is_file():
            return candidate
    return None
