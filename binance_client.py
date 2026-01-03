"""
币安客户端 - 优化版（批量处理 + 订单簿深度）
"""
import asyncio
import json
from typing import Dict, Optional, Callable, Set, List, Tuple
from datetime import datetime
from collections import deque
import aiohttp
import websockets
from loguru import logger

from models import MarketType, TickerData, SpreadData, PriceHistory, TokenInfo, OrderBookData


class BinanceClient:
    """币安客户端 - 优化版"""
    
    SPOT_REST = "https://api.binance.com"
    FUTURES_REST = "https://fapi.binance.com"
    
    def __init__(self):
        self.spot_prices: Dict[str, float] = {}
        self.futures_prices: Dict[str, float] = {}
        self.funding_rates: Dict[str, float] = {}
        self.next_funding_times: Dict[str, datetime] = {}
        
        # 24h数据
        self.spot_24h: Dict[str, dict] = {}
        self.futures_24h: Dict[str, dict] = {}
        
        self.spot_history: Dict[str, PriceHistory] = {}
        self.futures_history: Dict[str, PriceHistory] = {}
        
        self.spot_symbols: Set[str] = set()
        self.futures_symbols: Set[str] = set()
        
        # 订单簿缓存
        self.spot_orderbook: Dict[str, OrderBookData] = {}
        self.futures_orderbook: Dict[str, OrderBookData] = {}
        
        self.on_spot_update: Optional[Callable] = None
        self.on_futures_update: Optional[Callable] = None
        self.on_spread_update: Optional[Callable] = None
        self.on_orderbook_update: Optional[Callable] = None  # 新增
        
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        
        # WebSocket 配置
        self._ws_config = {
            'ping_interval': 20,
            'ping_timeout': 20,
            'close_timeout': 10,
            'max_size': 10 * 1024 * 1024,
        }
        
        # 批量处理配置
        self._batch_size = 50
        self._batch_interval = 0.1
        
        # 消息队列
        self._spot_queue: asyncio.Queue = asyncio.Queue()
        self._futures_queue: asyncio.Queue = asyncio.Queue()
        
        # 订单簿检查队列（存储需要检查的symbol）
        self._orderbook_check_queue: asyncio.Queue = asyncio.Queue()
        
        # 统计
        self._spot_msg_count = 0
        self._futures_msg_count = 0
        self._orderbook_check_count = 0
        self._last_stats_time = datetime.now()
        
        # 订单簿检查间隔（每个symbol至少间隔多久才检查一次）
        self._orderbook_check_interval = 30  # 秒
        self._last_orderbook_check: Dict[str, datetime] = {}
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def fetch_symbols(self):
        """获取交易对"""
        session = await self._get_session()
        
        try:
            async with session.get(f"{self.SPOT_REST}/api/v3/exchangeInfo") as resp:
                data = await resp.json()
                for s in data['symbols']:
                    if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT':
                        self.spot_symbols.add(s['symbol'])
            logger.info(f"现货交易对: {len(self.spot_symbols)}")
        except Exception as e:
            logger.error(f"获取现货交易对失败: {e}")
        
        try:
            async with session.get(f"{self.FUTURES_REST}/fapi/v1/exchangeInfo") as resp:
                data = await resp.json()
                for s in data['symbols']:
                    if s['status'] == 'TRADING' and s['quoteAsset'] == 'USDT':
                        self.futures_symbols.add(s['symbol'])
            logger.info(f"合约交易对: {len(self.futures_symbols)}")
        except Exception as e:
            logger.error(f"获取合约交易对失败: {e}")
    
    async def fetch_24h_tickers(self):
        """获取24小时行情"""
        session = await self._get_session()
        
        try:
            async with session.get(f"{self.SPOT_REST}/api/v3/ticker/24hr") as resp:
                data = await resp.json()
                for item in data:
                    symbol = item['symbol']
                    if symbol.endswith('USDT'):
                        self.spot_24h[symbol] = {
                            'price': float(item['lastPrice']),
                            'change': float(item['priceChange']),
                            'change_percent': float(item['priceChangePercent']),
                            'high': float(item['highPrice']),
                            'low': float(item['lowPrice']),
                            'volume': float(item['volume']),
                            'quote_volume': float(item['quoteVolume']),
                            'trades': int(item['count']),
                        }
            logger.debug(f"获取现货24h数据: {len(self.spot_24h)}")
        except Exception as e:
            logger.error(f"获取现货24h失败: {e}")
        
        try:
            async with session.get(f"{self.FUTURES_REST}/fapi/v1/ticker/24hr") as resp:
                data = await resp.json()
                for item in data:
                    symbol = item['symbol']
                    if symbol.endswith('USDT'):
                        self.futures_24h[symbol] = {
                            'price': float(item['lastPrice']),
                            'change': float(item['priceChange']),
                            'change_percent': float(item['priceChangePercent']),
                            'high': float(item['highPrice']),
                            'low': float(item['lowPrice']),
                            'volume': float(item['volume']),
                            'quote_volume': float(item['quoteVolume']),
                        }
            logger.debug(f"获取合约24h数据: {len(self.futures_24h)}")
        except Exception as e:
            logger.error(f"获取合约24h失败: {e}")
    
    async def fetch_funding_rates(self):
        """获取资金费率"""
        session = await self._get_session()
        
        try:
            async with session.get(f"{self.FUTURES_REST}/fapi/v1/premiumIndex") as resp:
                data = await resp.json()
                for item in data:
                    symbol = item['symbol']
                    self.funding_rates[symbol] = float(item['lastFundingRate']) * 100
                    if item['nextFundingTime']:
                        self.next_funding_times[symbol] = datetime.fromtimestamp(
                            item['nextFundingTime'] / 1000
                        )
        except Exception as e:
            logger.error(f"获取资金费率失败: {e}")
    
    async def fetch_orderbook(self, symbol: str, market: MarketType = MarketType.SPOT, 
                              limit: int = 20) -> Optional[OrderBookData]:
        """
        获取订单簿深度数据
        """
        session = await self._get_session()
        
        try:
            if market == MarketType.SPOT:
                url = f"{self.SPOT_REST}/api/v3/depth?symbol={symbol}&limit={limit}"
            else:
                url = f"{self.FUTURES_REST}/fapi/v1/depth?symbol={symbol}&limit={limit}"
            
            async with session.get(url) as resp:
                data = await resp.json()
                
                bids = [(float(p), float(q)) for p, q in data.get('bids', [])]
                asks = [(float(p), float(q)) for p, q in data.get('asks', [])]
                
                # 计算统计数据
                current_price = self.spot_prices.get(symbol, 0) if market == MarketType.SPOT else self.futures_prices.get(symbol, 0)
                
                max_bid_order = 0
                max_bid_price = 0
                total_bid_value = 0
                
                for price, qty in bids:
                    value = price * qty
                    total_bid_value += value
                    if value > max_bid_order:
                        max_bid_order = value
                        max_bid_price = price
                
                max_ask_order = 0
                max_ask_price = 0
                total_ask_value = 0
                
                for price, qty in asks:
                    value = price * qty
                    total_ask_value += value
                    if value > max_ask_order:
                        max_ask_order = value
                        max_ask_price = price
                
                bid_ask_ratio = total_bid_value / total_ask_value if total_ask_value > 0 else 1.0
                
                orderbook = OrderBookData(
                    symbol=symbol,
                    bids=bids,
                    asks=asks,
                    max_bid_order=max_bid_order,
                    max_ask_order=max_ask_order,
                    max_bid_price=max_bid_price,
                    max_ask_price=max_ask_price,
                    total_bid_value=total_bid_value,
                    total_ask_value=total_ask_value,
                    bid_ask_ratio=bid_ask_ratio,
                    market_type=market,
                )
                
                # 缓存
                if market == MarketType.SPOT:
                    self.spot_orderbook[symbol] = orderbook
                else:
                    self.futures_orderbook[symbol] = orderbook
                
                return orderbook
                
        except Exception as e:
            logger.error(f"获取订单簿失败 {symbol}: {e}")
            return None
    
    async def check_orderbook_for_symbol(self, symbol: str, market: MarketType = MarketType.SPOT):
        """
        检查指定symbol的订单簿并触发回调
        """
        # 检查是否需要限流
        now = datetime.now()
        last_check = self._last_orderbook_check.get(symbol)
        if last_check and (now - last_check).total_seconds() < self._orderbook_check_interval:
            return
        
        self._last_orderbook_check[symbol] = now
        self._orderbook_check_count += 1
        
        orderbook = await self.fetch_orderbook(symbol, market, limit=20)
        
        if orderbook and self.on_orderbook_update:
            await self.on_orderbook_update(orderbook)
    
    def get_token_info(self, symbol: str, market: MarketType = MarketType.SPOT) -> Optional[TokenInfo]:
        """获取代币信息"""
        data_24h = self.spot_24h if market == MarketType.SPOT else self.futures_24h
        
        if symbol not in data_24h:
            return None
        
        d = data_24h[symbol]
        return TokenInfo(
            symbol=symbol,
            base_asset=symbol.replace('USDT', ''),
            price=d['price'],
            price_change_24h=d['change'],
            price_change_percent_24h=d['change_percent'],
            high_24h=d['high'],
            low_24h=d['low'],
            volume_24h=d['volume'],
            quote_volume_24h=d['quote_volume'],
            trades_24h=d.get('trades', 0),
        )
    
    def get_top_gainers(self, limit: int = 10, market: MarketType = MarketType.SPOT) -> List[Tuple[str, float, float, float]]:
        """获取涨幅榜"""
        data_24h = self.spot_24h if market == MarketType.SPOT else self.futures_24h
        
        items = [(s, d['price'], d['change_percent'], d['quote_volume']) 
                 for s, d in data_24h.items() if d['quote_volume'] > 1000000]
        
        items.sort(key=lambda x: x[2], reverse=True)
        return items[:limit]
    
    def get_top_losers(self, limit: int = 10, market: MarketType = MarketType.SPOT) -> List[Tuple[str, float, float, float]]:
        """获取跌幅榜"""
        data_24h = self.spot_24h if market == MarketType.SPOT else self.futures_24h
        
        items = [(s, d['price'], d['change_percent'], d['quote_volume']) 
                 for s, d in data_24h.items() if d['quote_volume'] > 1000000]
        
        items.sort(key=lambda x: x[2])
        return items[:limit]
    
    def get_top_volume(self, limit: int = 10, market: MarketType = MarketType.SPOT) -> List[Tuple[str, float, float, float]]:
        """获取成交额榜"""
        data_24h = self.spot_24h if market == MarketType.SPOT else self.futures_24h
        
        items = [(s, d['price'], d['change_percent'], d['quote_volume']) 
                 for s, d in data_24h.items()]
        
        items.sort(key=lambda x: x[3], reverse=True)
        return items[:limit]
    
    def get_top_spreads(self, limit: int = 10) -> List[Tuple[str, float, float, float, float]]:
        """获取差价榜"""
        spreads = []
        
        common = self.spot_symbols & self.futures_symbols
        for symbol in common:
            if symbol in self.spot_prices and symbol in self.futures_prices:
                spot = self.spot_prices[symbol]
                futures = self.futures_prices[symbol]
                if spot > 0:
                    spread_pct = ((futures - spot) / spot) * 100
                    funding = self.funding_rates.get(symbol, 0)
                    spreads.append((symbol, spot, futures, spread_pct, funding))
        
        spreads.sort(key=lambda x: abs(x[3]), reverse=True)
        return spreads[:limit]
    
    def get_top_funding_rates(self, limit: int = 10, positive: bool = True) -> List[Tuple[str, float, float]]:
        """获取资金费率榜"""
        items = [(s, r, self.futures_prices.get(s, 0)) 
                 for s, r in self.funding_rates.items() if s in self.futures_prices]
        
        if positive:
            items.sort(key=lambda x: x[1], reverse=True)
        else:
            items.sort(key=lambda x: x[1])
        
        return items[:limit]
    
    async def _connect_spot_ws(self):
        """连接现货WebSocket"""
        symbols = list(self.spot_symbols)[:200]
        streams = [f"{s.lower()}@miniTicker" for s in symbols]
        url = f"wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)
        
        reconnect_delay = 5
        max_delay = 60
        
        while self._running:
            try:
                logger.info("正在连接现货WebSocket...")
                
                async with websockets.connect(
                    url,
                    ping_interval=self._ws_config['ping_interval'],
                    ping_timeout=self._ws_config['ping_timeout'],
                    close_timeout=self._ws_config['close_timeout'],
                    max_size=self._ws_config['max_size'],
                ) as ws:
                    logger.info("✅ 现货WebSocket已连接")
                    reconnect_delay = 5
                    
                    async for msg in ws:
                        if not self._running:
                            break
                        await self._spot_queue.put(msg)
                        
            except websockets.ConnectionClosedOK:
                logger.info("现货WS正常关闭")
                if not self._running:
                    break
            except websockets.ConnectionClosedError as e:
                logger.warning(f"现货WS连接关闭: code={e.code}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"现货WS错误: {type(e).__name__}: {e}")
            
            if self._running:
                logger.info(f"现货WS {reconnect_delay}秒后重连...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, max_delay)
    
    async def _connect_futures_ws(self):
        """连接合约WebSocket"""
        symbols = list(self.futures_symbols)[:200]
        streams = [f"{s.lower()}@miniTicker" for s in symbols]
        url = f"wss://fstream.binance.com/stream?streams=" + "/".join(streams)
        
        reconnect_delay = 5
        max_delay = 60
        
        while self._running:
            try:
                logger.info("正在连接合约WebSocket...")
                
                async with websockets.connect(
                    url,
                    ping_interval=self._ws_config['ping_interval'],
                    ping_timeout=self._ws_config['ping_timeout'],
                    close_timeout=self._ws_config['close_timeout'],
                    max_size=self._ws_config['max_size'],
                ) as ws:
                    logger.info("✅ 合约WebSocket已连接")
                    reconnect_delay = 5
                    
                    async for msg in ws:
                        if not self._running:
                            break
                        await self._futures_queue.put(msg)
                        
            except websockets.ConnectionClosedOK:
                logger.info("合约WS正常关闭")
                if not self._running:
                    break
            except websockets.ConnectionClosedError as e:
                logger.warning(f"合约WS连接关闭: code={e.code}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"合约WS错误: {type(e).__name__}: {e}")
            
            if self._running:
                logger.info(f"合约WS {reconnect_delay}秒后重连...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, max_delay)
    
    async def _batch_processor_spot(self):
        """批量处理现货消息"""
        while self._running:
            try:
                batch = {}
                
                try:
                    while len(batch) < self._batch_size:
                        msg = await asyncio.wait_for(
                            self._spot_queue.get(), 
                            timeout=self._batch_interval
                        )
                        data = json.loads(msg)
                        if 'data' in data:
                            data = data['data']
                        symbol = data.get('s')
                        if symbol:
                            batch[symbol] = data
                except asyncio.TimeoutError:
                    pass
                
                for symbol, data in batch.items():
                    await self._process_spot_data(data)
                    self._spot_msg_count += 1
                
                await self._log_stats()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"现货批量处理错误: {e}")
    
    async def _batch_processor_futures(self):
        """批量处理合约消息"""
        while self._running:
            try:
                batch = {}
                
                try:
                    while len(batch) < self._batch_size:
                        msg = await asyncio.wait_for(
                            self._futures_queue.get(), 
                            timeout=self._batch_interval
                        )
                        data = json.loads(msg)
                        if 'data' in data:
                            data = data['data']
                        symbol = data.get('s')
                        if symbol:
                            batch[symbol] = data
                except asyncio.TimeoutError:
                    pass
                
                for symbol, data in batch.items():
                    await self._process_futures_data(data)
                    self._futures_msg_count += 1
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"合约批量处理错误: {e}")
    
    async def _orderbook_checker(self):
        """订单簿检查器 - 从队列获取symbol并检查"""
        while self._running:
            try:
                # 从队列获取需要检查的symbol
                item = await asyncio.wait_for(
                    self._orderbook_check_queue.get(),
                    timeout=1.0
                )
                symbol, market = item
                await self.check_orderbook_for_symbol(symbol, market)
                
                # 限制检查速率，避免API限制
                await asyncio.sleep(0.1)
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"订单簿检查错误: {e}")
    
    async def _log_stats(self):
        """记录统计信息"""
        now = datetime.now()
        if (now - self._last_stats_time).total_seconds() >= 60:
            logger.info(
                f"📡 WS统计: 现货={self._spot_msg_count}/分, "
                f"合约={self._futures_msg_count}/分, "
                f"订单簿检查={self._orderbook_check_count}/分"
            )
            self._spot_msg_count = 0
            self._futures_msg_count = 0
            self._orderbook_check_count = 0
            self._last_stats_time = now
    
    async def _process_spot_data(self, data: dict):
        """处理现货数据"""
        try:
            symbol = data.get('s')
            price = float(data.get('c', 0))
            volume = float(data.get('v', 0))
            high = float(data.get('h', 0))
            low = float(data.get('l', 0))
            
            if not symbol or price <= 0:
                return
            
            self.spot_prices[symbol] = price
            
            if symbol in self.spot_24h:
                self.spot_24h[symbol]['price'] = price
                if high > 0:
                    self.spot_24h[symbol]['high'] = max(self.spot_24h[symbol]['high'], high)
                if low > 0 and self.spot_24h[symbol]['low'] > 0:
                    self.spot_24h[symbol]['low'] = min(self.spot_24h[symbol]['low'], low)
            
            if symbol not in self.spot_history:
                self.spot_history[symbol] = PriceHistory(symbol, MarketType.SPOT)
            self.spot_history[symbol].add(price, volume)
            
            if self.on_spot_update:
                ticker = self._make_ticker(symbol, MarketType.SPOT)
                await self.on_spot_update(ticker)
            
            if symbol in self.futures_prices and self.on_spread_update:
                spread = self._make_spread(symbol)
                await self.on_spread_update(spread)
                
        except Exception as e:
            pass
    
    async def _process_futures_data(self, data: dict):
        """处理合约数据"""
        try:
            symbol = data.get('s')
            price = float(data.get('c', 0))
            volume = float(data.get('v', 0))
            
            if not symbol or price <= 0:
                return
            
            self.futures_prices[symbol] = price
            
            if symbol not in self.futures_history:
                self.futures_history[symbol] = PriceHistory(symbol, MarketType.FUTURES)
            self.futures_history[symbol].add(price, volume)
            
            if self.on_futures_update:
                ticker = self._make_ticker(symbol, MarketType.FUTURES)
                await self.on_futures_update(ticker)
            
            if symbol in self.spot_prices and self.on_spread_update:
                spread = self._make_spread(symbol)
                await self.on_spread_update(spread)
                
        except Exception as e:
            pass
    
    def _make_ticker(self, symbol: str, market: MarketType) -> TickerData:
        """创建Ticker"""
        if market == MarketType.SPOT:
            price = self.spot_prices.get(symbol, 0)
            history = self.spot_history.get(symbol)
            data_24h = self.spot_24h.get(symbol, {})
        else:
            price = self.futures_prices.get(symbol, 0)
            history = self.futures_history.get(symbol)
            data_24h = self.futures_24h.get(symbol, {})
        
        ticker = TickerData(
            symbol=symbol, 
            price=price, 
            market_type=market,
            high_24h=data_24h.get('high', 0),
            low_24h=data_24h.get('low', 0),
            volume_24h=data_24h.get('volume', 0),
            quote_volume_24h=data_24h.get('quote_volume', 0),
            price_change_24h=data_24h.get('change_percent', 0),
        )
        
        if history:
            ticker.price_change_1m = history.get_change(1) or 0
            ticker.price_change_5m = history.get_change(5) or 0
            ticker.price_change_15m = history.get_change(15) or 0
            ticker.price_change_1h = history.get_change(60) or 0
            ticker.volume_change_ratio = history.get_volume_ratio(5)
        
        return ticker
    
    def _make_spread(self, symbol: str) -> SpreadData:
        """创建差价数据"""
        spot = self.spot_prices.get(symbol, 0)
        futures = self.futures_prices.get(symbol, 0)
        spread_pct = ((futures - spot) / spot * 100) if spot > 0 else 0
        
        return SpreadData(
            symbol=symbol,
            spot_price=spot,
            futures_price=futures,
            spread_percent=spread_pct,
            funding_rate=self.funding_rates.get(symbol, 0),
            next_funding_time=self.next_funding_times.get(symbol),
        )
    
    def queue_orderbook_check(self, symbol: str, market: MarketType = MarketType.SPOT):
        """将symbol加入订单簿检查队列"""
        try:
            self._orderbook_check_queue.put_nowait((symbol, market))
        except asyncio.QueueFull:
            pass  # 队列满了就跳过
    
    async def start(self):
        """启动"""
        self._running = True
        logger.info("启动 Binance 客户端...")
        
        await self.fetch_symbols()
        await self.fetch_24h_tickers()
        await self.fetch_funding_rates()
        
        await asyncio.gather(
            self._connect_spot_ws(),
            self._connect_futures_ws(),
            self._batch_processor_spot(),
            self._batch_processor_futures(),
            self._orderbook_checker(),  # 新增订单簿检查器
            self._periodic_update(),
            return_exceptions=True
        )
    
    async def _periodic_update(self):
        """定期更新"""
        while self._running:
            try:
                await asyncio.sleep(60)
                if self._running:
                    await self.fetch_24h_tickers()
                    await self.fetch_funding_rates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定期更新错误: {e}")
    
    async def stop(self):
        """停止"""
        logger.info("停止 Binance 客户端...")
        self._running = False
        
        if self._session and not self._session.closed:
            await self._session.close()