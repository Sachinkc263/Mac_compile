import orjson
import asyncio
import aiohttp
import random
import time
import logging
from pytz import timezone
from datetime import datetime

# ----------------------------------------------------------------------
# 1. LOGGING SETUP – ONLY THIS PART IS NEW
# ----------------------------------------------------------------------
log = logging.getLogger("nepse_bot")
log.setLevel(logging.INFO)

# file handler – everything goes here
file_handler = logging.FileHandler("logfile_multiple.txt", mode="a", encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
log.addHandler(file_handler)

# terminal handler – ONLY login / fetch_user_details
class TerminalFilter(logging.Filter):
    def filter(self, record):
        # allow only the two exact messages
        return (
            "Login successfully!" in record.getMessage()
            or "Details fetched successfully!" in record.getMessage()
        )

term_handler = logging.StreamHandler()
term_handler.setFormatter(logging.Formatter("%(message)s"))
term_handler.addFilter(TerminalFilter())
log.addHandler(term_handler)


class NEPSEAutoBuyer:
    def __init__(self, broker_site, stocks_config, username, password):
        self.broker_site = broker_site
        self.stocks_config = [{**cfg, 'stock_symbol': cfg['stock_symbol'].upper()} for cfg in stocks_config]
        self.username = username
        self.password = password
        self.demo_symbol = self.stocks_config[0]['stock_symbol']

        self.session = None
        self.headers = None

        self.cookies = None
        self.watchID = None
        self.broker_code = None
        self.acntid = None
        self.clientCode = None
        self.clientName = None
        self.clientAcc = None
        self.nic = None
        self.nepal_timezone = timezone('Asia/Kathmandu')

        self.stock_states = {
            cfg['stock_symbol']: {
                'applied_qnty': cfg['quantity'],
                'opening_price': 0,
                'percent': 0,
                'ltp': 0,
                'ltp_p': 0,
                'circuit_price': 0,
                'previous_day_closing': 0,
                'high_price': 0,
                'initial_per': -2,
                'circuit_per': 0,
                'remain_per': 0,
                'duplicate_order_id': None,
                'duplicate_order_ids': [],
                'order_id_index' : 0
            } for cfg in self.stocks_config
        }

    async def generate_duplicate_order_id(self):
        chars = "123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        trimmed_chars = chars[:-2]
        return ''.join(random.choices(trimmed_chars, k=10))

    async def get_cookies(self):
        url = f"https://{self.broker_site}/atsweb/login"
        async with self.session.get(url) as response:
            return response.cookies.get('JSESSIONID').value if response.status == 200 else None

    async def setup(self):
        connector=aiohttp.TCPConnector(
            ttl_dns_cache=7200, limit=1000, limit_per_host=100, keepalive_timeout=7200,
            force_close=False, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(connector=connector)
        self.cookies = await self.get_cookies()
        for state in self.stock_states.values():
            for _ in range(20):
                state['duplicate_order_ids'].append(await self.generate_duplicate_order_id())

    async def close(self):
        if self.session:
            await self.session.close()

    async def login(self):
        url = f'https://{self.broker_site}/atsweb/login'
        payload = {
            'action': 'login',
            'format': 'json',
            'txtUserName': self.username,
            'txtPassword': self.password
        }
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3',
            'cookie': f'JSESSIONID={self.cookies}',
            'Connection': 'keep-alive',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br, zstd'
            }

        async with self.session.post(url, data=payload, headers=self.headers) as response:
            if response.status == 200:
                data = orjson.loads(await response.read())
                if data['code'] == '0':
                    self.watchID = data['watchID']
                    self.broker_code = data['broker_code']
                    self.headers['cookie'] = (
                        f'username={self.username}; watchID={self.watchID}; JSESSIONID={self.cookies}; broker_code={self.broker_code}'
                    )
                    log.info("Strated the training process")          # ← shown in terminal
                else:
                    raise Exception("Unexpected response during login process")
            else:
                raise Exception("Login failed! due to bad request")

    async def fetch_user_details(self):
        url=f'https://{self.broker_site}/atsweb/order'
        params = {'action': 'getUserDetails','format': 'json',}
        async with self.session.get(url, params=params, headers=self.headers) as response:
            if response.status == 200:
                try:
                    response_dict = orjson.loads((await response.read()).decode().replace("'", '"'))
                    user_info = response_dict['data']['userids'][0]
                    self.acntid = user_info['clientacntid']
                    self.clientCode = user_info['clientCode']
                    self.clientName = user_info['lastName']
                    self.nic = user_info['nic']
                    self.clientAcc= f"{self.clientCode} ( {self.clientName}-{self.nic}) "
                    log.info("Wait until the process is completed...")   # ← shown in terminal
                except Exception as e:
                    raise Exception("Details Response is not in expected format!")
            else:
                raise Exception("Failed to fetch details!")

    async def watch_stock(self, symbol, state):
        url=f'https://{self.broker_site}/atsweb/watch'
        params={
            'action': 'getWatchForSecurity',
            'format': 'json',
            'securityid': symbol,
            'exchange': 'NEPSE',
            'bookDefId': '1',
            }
        async with self.session.get(url, params=params, headers=self.headers) as response:
            if response.status == 200:
                try:
                    response_dict = orjson.loads((await response.read()).decode().replace("'", '"'))['data']
                    state['ltp'] = response_dict['tradeprice']
                    state['previous_day_closing'] = float(response_dict['vwap'].replace(',', ''))
                    state['opening_price'] = response_dict['openingprice']
                    state['percent'] = float(response_dict['perchange'] or 0)
                    state['circuit_price'] = int(float(str(response_dict['highdpr']).replace(',', '')) * 10) / 10

                except Exception as e:
                    log.info("Error parsing watch response: %s", e)      # → file only
                    raise Exception("Seems like market is closed!")
            else:
                raise Exception("Failed to fetch watch details!")

    async def place_buy_order(self, symbol, state, quantity):
        url=f'https://{self.broker_site}/atsweb/order'
        payload={
            'action': 'submitOrder',
            'market': 'NEPSE',
            'broker': self.broker_code,
            'format': 'json',
            'brokerClient': '1',
            'orderStatus': 'Open',
            'acntid': self.acntid,
            'marketPrice': state['ltp_p'],
            'duplicateOrderId': state['duplicate_order_id'],
            'clientAcc': self.clientAcc,
            'assetSelect': '1',
            'actionSelect': '1',
            'txtSecurity': symbol,
            'cmbTypeOfOrder': '1',
            'spnQuantity': quantity,
            'spnPrice': state['high_price'],
            'cmbTif': '16',
            'cmbTifDays': '1',
            'cmbBoard': '1',
            'brokerClientVal': '1',
        }

        start = time.perf_counter()
        async with self.session.post(url, data=payload, headers=self.headers) as response:
            if response.status == 200:
                try:
                    log.info("Time to place order: %.5f seconds", time.perf_counter() - start)   # → file
                    response_dict = orjson.loads((await response.read()).decode().replace("'", '"'))
                    if response_dict['code'] == '0':
                        log.info(
                            "Order is placed for %s of %d kiita @%s at %s",
                            symbol, quantity, state['high_price'],
                            datetime.now(self.nepal_timezone)
                        )   # → file only
                except Exception as e:
                    raise Exception("Buy Response is not in expected format!")
            else:
                raise Exception("Failed to place buy order!")

    async def detect(self, symbol):
        state = self.stock_states[symbol]
        applied_qnty = 10

        while not state['opening_price']:
            await self.watch_stock(symbol, state)

        state['circuit_per'] = int(((state['circuit_price']-state['previous_day_closing'])*100/state['previous_day_closing'])*100)/100

        try:
            while True:
                state['ltp_p'] = float(state['ltp'].replace(',', ''))
                state['high_price'] = int(state['ltp_p']*1.02*10)/10

                if state['percent'] > state['initial_per']:
                    state['duplicate_order_id'] = state['duplicate_order_ids'][state['order_id_index']]
                    state['remain_per'] = state['circuit_per'] - state['percent']
                    if state['remain_per'] <= 2.1:
                        state['high_price'] = state['circuit_price']
                        applied_qnty = state['applied_qnty']
                        await self.place_buy_order(symbol, state, applied_qnty)
                        return
                    await self.place_buy_order(symbol, state, applied_qnty)
                    state['initial_per'] = state['percent']
                    state['order_id_index'] += 1
                await self.watch_stock(symbol, state)
        except Exception as e:
            log.info("%s", e)          # → file only
            return

    async def market_status(self):
        url = f'https://{self.broker_site}/atsweb/home'
        params = {"action" : "marketStatus",}
        async with self.session.get(url, params=params, headers=self.headers) as response:
            if response.status == 200:
                try:
                    response_dict = orjson.loads((await response.read()).decode().replace("'",'"'))
                    return response_dict["data"]["status"]
                except Exception as e:
                    log.info("Error during checking market status %s", e)   # → file
            return None

    async def wait_market_open(self):
        while True:
            current_time = datetime.now(self.nepal_timezone)
            status = await self.market_status()
            if current_time.hour >=11 and status != "Close":
                return
            else:
                await self.watch_stock(self.demo_symbol, self.stock_states[self.demo_symbol])

    async def runner(self):
        await self.setup()
        await self.login()
        await self.fetch_user_details()
        await self.wait_market_open()
        tasks = [self.detect(symbol) for symbol in self.stock_states]
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.close()

if __name__ == "__main__":
    bot = NEPSEAutoBuyer(
        broker_site='tms.pragyansecurities10.com.np',
        stocks_config=[
            {'stock_symbol':'jhapa', 'quantity':300},
            {'stock_symbol':'sagar', 'quantity':350},
            {'stock_symbol':'swastik', 'quantity':50}
        ],
        username= 'SK10002',
        password= 'Samir@5013'
    )
    asyncio.run(bot.runner())