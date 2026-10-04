from app import liquid
from app.liquid import LiquidRegistry, asset_variants, enabled_sources
from app.models import SymbolInfo, Ticker
from app.scanner import build_universe, pair_url
from app.settings import LiquidSettings, UniverseSettings


class _NoStorage:
    async def load_liquid_cache(self):
        return {}

    async def save_liquid_cache(self, *args):
        pass


def test_asset_variants_strip_multiplier_and_aliases():
    assert asset_variants("1000PEPE") == {"1000PEPE", "PEPE"}
    assert asset_variants("1INCH") == {"1INCH"}
    assert asset_variants("xbt") == {"BTC"}


def test_venue_parsers():
    assert liquid.parse_binance(
        {"symbols": [{"baseAsset": "BTC", "status": "TRADING"}, {"baseAsset": "OLD", "status": "BREAK"}]}
    ) == {"BTC"}
    assert liquid.parse_bybit({"result": {"list": [{"baseCoin": "ETH", "status": "Trading"}]}}) == {"ETH"}
    assert liquid.parse_okx_spot(
        {"data": [{"baseCcy": "SOL", "state": "live"}, {"baseCcy": "X", "state": "suspend"}]}
    ) == {"SOL"}
    assert liquid.parse_okx_swap({"data": [{"instFamily": "DOGE-USDT", "state": "live"}]}) == {"DOGE"}
    assert liquid.parse_bitget_spot({"data": [{"baseCoin": "TON", "status": "online"}]}) == {"TON"}
    assert liquid.parse_coinbase([{"base_currency": "ADA", "status": "online", "trading_disabled": False}]) == {"ADA"}
    assert liquid.parse_upbit([{"market": "KRW-XRP"}, {"market": "BTC-XRP"}]) == {"XRP"}
    assert liquid.parse_kraken({"result": {"XXBTZUSD": {"wsname": "XBT/USD", "status": "online"}}}) == {"XBT"}
    assert liquid.parse_kucoin_futures({"data": [{"baseCurrency": "XBT", "status": "Open"}]}) == {"XBT"}
    assert liquid.parse_htx_spot({"data": [{"bc": "trx", "state": "online"}]}) == {"trx"}
    assert liquid.parse_htx_futures({"data": [{"symbol": "BTC", "contract_status": 1}]}) == {"BTC"}


def test_enabled_sources_follow_settings():
    cfg = LiquidSettings()
    keys = enabled_sources(cfg)
    assert "binance:spot" in keys and "binance:futures" in keys
    assert "coinbase:spot" in keys and "coinbase:futures" not in keys
    assert not any(k.startswith("kucoin") for k in keys)
    cfg.binance.futures = False
    assert "binance:futures" not in enabled_sources(cfg)


def test_registry_listed_on():
    reg = LiquidRegistry(_NoStorage())
    reg._set("binance:futures", ["1000PEPE", "BTC"], 0)
    reg._set("kucoin:spot", ["RARE"], 0)
    cfg = LiquidSettings(extra_assets=["SCAM"])
    assert reg.listed_on("PEPE", cfg) == ["binance:futures"]
    assert reg.listed_on("RARE", cfg) == []  # KuCoin выключен по умолчанию
    assert reg.listed_on("SCAM", cfg) == ["вручную"]
    cfg.binance.enabled = False
    assert reg.listed_on("PEPE", cfg) == []


def _market():
    symbols = {
        s: SymbolInfo(s, s.removesuffix("USDT"), "USDT")
        for s in ["GEMUSDT", "BTCUSDT", "DUSTUSDT", "WHALEUSDT", "BTC3LUSDT", "BANNEDUSDT", "PINNEDUSDT"]
    }
    symbols["GEMBTC"] = SymbolInfo("GEMBTC", "GEM", "BTC")
    vol = {
        "GEMUSDT": 50_000,
        "BTCUSDT": 50_000,
        "DUSTUSDT": 10,
        "WHALEUSDT": 50_000_000,
        "BTC3LUSDT": 50_000,
        "BANNEDUSDT": 50_000,
    }
    tickers = {s: Ticker(s, 1.0, 0.99, 1.01, v) for s, v in vol.items()}
    return symbols, tickers


def test_build_universe_filters():
    symbols, tickers = _market()
    cfg = UniverseSettings(blacklist=["banned"], whitelist=["PINNED"])
    selected, stats = build_universe(symbols, tickers, cfg, lambda base: ["binance:spot"] if base == "BTC" else [])
    assert selected == ["GEMUSDT", "PINNEDUSDT"]
    assert stats == {
        "total": 7,
        "blacklist": 1,
        "liquid": 1,
        "low_volume": 1,
        "high_volume": 1,
        "leveraged": 1,
        "selected": 2,
    }


def test_build_universe_can_keep_liquid_coins():
    symbols, tickers = _market()
    cfg = UniverseSettings(exclude_listed_on_liquid=False)
    selected, _ = build_universe(symbols, tickers, cfg, lambda base: ["binance:spot"])
    assert "BTCUSDT" in selected


def test_pair_url():
    info = SymbolInfo("ABC_USDT", "ABC", "USDT")
    assert pair_url("https://www.gate.com/trade/{base}_{quote}", info) == "https://www.gate.com/trade/ABC_USDT"
    assert pair_url("https://x/{symbol}", info) == "https://x/ABC_USDT"
    assert pair_url("https://x/{oops}", info) == "https://x/{oops}"
