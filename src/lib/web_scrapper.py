import datetime
import time
import requests
import threading
import numpy as np

from typing import Optional, Union
from .settings import APP_SETTINGS
from .logging import logger
from .indicators import (
    IndicatorsError,
    sma,
    vii_stop,
    rsi,
    NotEnoughDataError,
    NotDataSeriesError,
)


class APIError(Exception):
    pass


class GeckoTerminalAPIError(Exception):
    pass


class DexscreenerAPIError(Exception):
    pass


class ScraperThread:

    sleep_time: int = 60
    history_limit: int = 500

    class StrategyError(Exception):
        pass

    class NotEnoughDataError(StrategyError):
        pass

    class SignalFinished(StrategyError):
        pass

    def __init__(self, network: str, token_address: str, *args, **kwargs):
        self._scraper = DexScraper()
        self._signal_start: Optional[int] = None
        self.network: str = network
        self._token_address: str = token_address
        self._pool_address: Optional[str] = None
        self._last_updated: Optional[int] = None
        self._response_history: list = (
            []
        )  # TODO: add fetch on init, from the API, if possible
        self._watch_list_keys: list = []

        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, args=args, kwargs=kwargs)
        self.close_prices = np.array([])  # close prices
        self.series = np.array([])  # tohlcv series

    def __repr__(self):
        return (
            f"ScraperThread(network={self.network}, token_address={self.token_address})"
        )

    def __str__(self):
        return (
            f"ScraperThread(network={self.network}, token_address={self.token_address})"
        )

    def __del__(self):
        self.stop()

    def __enter__(self):
        return self

    # def __exit__(self, exc_type, exc_value, traceback):
    #     if not self._stop_event.is_set():
    #         logger.error("Thread not stopped, crashed, or exited unexpectedly")

    def __eq__(self, other):
        return (
            self.network == other.network and self.token_address == other.token_address
        )

    def _initialize_price_history(self):
        # required: network, pool_address
        # optional: timeframe, from_timestamp, to_timestamp
        try:
            response = self.scraper.get_gecko_data_from_overkill(
                {
                    "network": self.network,
                    "pool_address": self.pool_address,
                    "timeframe": "minute",
                }
            )
        except requests.RequestException as e:
            logger.warning(f"could not initialize price history: {e}")
            self.signal_start = int(time.time())
        else:
            response_data = response.get("result", [])
            try:
                self.signal_start = self._get_int_timestamp(
                    response_data[0].get("timestamp")
                )
            except (IndexError, KeyError):
                self.signal_start = int(time.time())
            try:
                self.response_history = response_data
                logger.info(
                    f"Price history initialized for {self.network} {self.pool_address}"
                )
            except IndicatorsError as e:
                logger.error(f"could not initialize price history: {e}")

    def is_token_still_trending(self):

        try:
            _, vii_stop_uptrend = vii_stop(src=self.series, length=19)
            rsi_val = rsi(src=self.close_prices, length=21)
            sma_val = sma(src=self.close_prices, length=1200)  # MA 20 on H1
        except (NotEnoughDataError, NotDataSeriesError):
            # TODO: liqiudity check after 4 hours
            if self.signal_start < int(time.time()) - 43200:  # 12 hours
                raise self.NotEnoughDataError("Not enough data to determine trend")
            return True
        except IndicatorsError as e:
            logger.error(f"IndicatorsError in is_token_still_trending: {e}")
            return True
        else:
            if rsi_val[0] < 70 and not vii_stop_uptrend:
                return False
            elif (
                sma_val[0] > self.close_prices[0]  # 1200 samples minimum required
                and self.signal_start < int(time.time()) - 21600
            ):  # 6 hours
                raise self.SignalFinished("Signal finished")
            return True

    @property
    def stop_event(self):
        return self._stop_event

    @stop_event.setter
    def stop_event(self, value: threading.Event):
        self._stop_event = value

    @property
    def signal_start(self):
        return self._signal_start

    @signal_start.setter
    def signal_start(self, value: int):
        logger.debug(f"Signal start set to {value}")
        self._signal_start = value

    @property
    def scraper(self):
        return self._scraper

    @property
    def pool_address(self):
        return self._pool_address

    @property
    def response_history(self):
        return self._response_history

    def _get_int_timestamp(self, timestamp: str):
        if isinstance(timestamp, int):
            return timestamp
        return int(
            datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S").timestamp()
        )

    @response_history.setter
    def response_history(self, value: Union[dict, list]):
        if isinstance(value, dict):
            if len(self.response_history) + 1 > self.history_limit:
                self._response_history.pop(0)
            self._response_history.append(value)
        elif isinstance(value, list):
            if len(self.response_history) + len(value) > self.history_limit:
                self._response_history = self._response_history[-self.history_limit :]
            self._response_history.extend(value)
        self.close_prices = np.array(
            [float(data["close"]) for data in self.response_history],
            dtype=np.float64,
        )
        self.series = np.array(
            [
                [
                    self._get_int_timestamp(data["timestamp"]),
                    float(data["open"]),
                    float(data["high"]),
                    float(data["low"]),
                    float(data["close"]),
                    float(data["volume"]),
                ]
                for data in self.response_history
            ],
            dtype=np.float64,
        )

    @pool_address.setter
    def pool_address(self, value: str):
        self._pool_address = value

    @property
    def last_updated(self):
        return self._last_updated

    @last_updated.setter
    def last_updated(self, value: int):
        self._last_updated = value

    @property
    def network(self):
        return self._network

    @network.setter
    def network(self, value: str):
        if value not in self.scraper.network_ids_for_gecko_terminal:
            raise GeckoTerminalAPIError(f"Invalid network: {value}")
        self._network = value

    @property
    def token_address(self):
        return self._token_address

    def _is_rugged(self, mc: Optional[float], price_change: dict, txs: dict, vol: dict):
        # mc = top_pool["market_cap_usd"]  # value / None
        # price_change = top_pool["price_change_percentage"] # m5, h1, h6, h24
        # txs = top_pool["transactions"]  # m5, m15, m30, h1, h24
        # "buys": 0,
        # "sells": 0,
        # "buyers": 0,
        # "sellers": 0
        # vol = top_pool["volume_usd"] # m5, h1, h6, h24
        if mc is None:
            return True

        for key, val in price_change.items():
            price_change[key] = float(val)
        if price_change["m5"] < -50:
            return True
        elif price_change["h1"] < -80:
            return True
        elif price_change["h6"] < -90 or price_change["h24"] < -95:
            return True
        tx_m5 = txs["m5"]["buys"] + txs["m5"]["sells"]
        makers_m5 = txs["m5"]["buyers"] + txs["m5"]["sellers"]
        if tx_m5 > 1000 and makers_m5 < 10:
            return True
        tx_h1 = txs["h1"]["buys"] + txs["h1"]["sells"]
        makers_h1 = txs["h1"]["buyers"] + txs["h1"]["sellers"]
        tx_h6 = txs["h6"]["buys"] + txs["h6"]["sells"]
        makers_h6 = txs["h6"]["buyers"] + txs["h6"]["sellers"]
        if tx_h1 == 0 and makers_h1 == 0:
            return True
        elif tx_h6 == 0 and makers_h6 == 0:
            return True
        vol_m5 = float(vol["m5"])
        vol_h1 = float(vol["h1"])
        vol_h6 = float(vol["h6"])
        if vol_m5 == 0.0 and vol_h1 == 0.0:
            return True
        elif vol_h6 == 0.0:
            return True

    def _run(self, *args, **kwargs):
        self.last_updated = int(time.time())
        # TODO: fetch self.response_history from overkill API - if possible (because maybe the thread was stopped and restarted)
        # TODO: loop to fix threading crashes
        # can crash: self.scraper.get_top_pool_from_gecko, self._initialize_price_history(), self.is_token_still_trending()
        if "token_platform_address" not in kwargs:
            _, self.pool_address, _, _, _, _, _ = self.scraper.get_top_pool_from_gecko(
                network=self.network, token_address=self.token_address
            )

        else:
            self.pool_address = kwargs["token_platform_address"]
        self._initialize_price_history()
        if (self.network not in self.scraper.network_ids_for_gecko_terminal) or (
            self.token_address is None
        ):
            logger.error("Invalid network or token address")
            return
        counter: int = 0
        while not self.stop_event.is_set():
            try:
                rest_api_data = self._get_data()
                self.last_updated = self._post_data(rest_api_data)
            except GeckoTerminalAPIError as e:
                logger.error(f"GeckoTerminalAPIError in ScraperThread: {e}")
            except APIError as e:
                logger.error(f"APIError in ScraperThread: {e}")
            except requests.RequestException as e:
                logger.error(f"RequestException in ScraperThread: {e}")
            except IndicatorsError as e:
                logger.error(f"IndicatorsError in ScraperThread: {e}")
            else:
                self.last_updated += self.sleep_time
            finally:
                try:
                    if not self.is_token_still_trending():
                        raise self.StrategyError("Token is not trending")
                    if counter % 10 == 0:
                        _, _, _, mc, price_change, txs, vol = (
                            self.scraper.get_top_pool_from_gecko(
                                network=self.network, token_address=self.token_address
                            )
                        )
                        if self._is_rugged(mc, price_change, txs, vol):
                            raise self.StrategyError("Token is rugged")
                        counter = 0
                    counter += 1
                except (
                    GeckoTerminalAPIError,
                    APIError,
                    requests.RequestException,
                ) as e:
                    logger.error(f"Request exception in ScraperThread: {e}")
                except self.StrategyError as e:
                    logger.info(f"Strategy: {e}")
                    token_name, token_ticker = kwargs.get("token_name"), kwargs.get(
                        "token_ticker"
                    )
                    try:
                        self._post_delete(
                            {
                                "token_name": token_name,
                                "token_ticker": token_ticker,
                                "comment": str(e),
                            }
                        )
                        logger.info(f"Token {token_name} deleted from watch list")
                        self.stop_event.set()
                    except requests.RequestException as e:
                        logger.error(f"Request exception in ScraperThread: {e}")
                if self.last_updated > int(time.time()):
                    time.sleep(self.last_updated - int(time.time()))
                else:
                    time.sleep(self.sleep_time)
                logger.info(f"Thread for {self.network} {self.pool_address} resumed")
        logger.info(f"Thread for {self.network} {self.pool_address} stopped")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def _get_data(self):
        tohlcv = self._scraper.get_ohlcv(
            network=self.network, pool_address=self.pool_address
        )
        logger.debug(tohlcv)
        logger.info(f"Data fetched for {self.network} {self.pool_address}")
        self.response_history = {
            "timestamp": tohlcv[0],
            "open": tohlcv[1],
            "high": tohlcv[2],
            "low": tohlcv[3],
            "close": tohlcv[4],
            "volume": tohlcv[5],
        }
        return tohlcv

    def _post_data(self, tohlcv):
        if tohlcv is None:
            logger.info("No data to post")
            return
        gecko_data = {
            "network": self.network,
            "pool_address": self.pool_address,
            "timeframe": "minute",
            "aggregation": "1",
            "timestamp": tohlcv[0],
            "currency": "usd",
            "token": "base",
            **self.response_history[-1],
        }
        self._scraper.post_gecko_data_to_overkill(data=gecko_data)
        logger.info(f"Data posted for {self.network} {self.pool_address}")
        return tohlcv[0]

    def _post_delete(self, token_data):
        return self._scraper.delete_coin_from_watch_list(token_data)


class DexThreadManager:
    scrappers: dict = {}  # dict for network_token-address
    threads_limit: int = 50

    def __init__(self):
        self.scraper = DexScraper()

    @property
    def watch_list(self):
        return self._watch_list

    @watch_list.setter
    def watch_list(self, value: list):
        if len(value) >= self.threads_limit:
            logger.error("Threads limit reached, setting maximum threads")
            value = value[: self.threads_limit]
        self._watch_list_keys = [
            f"{token_data.get('token_network')}_{token_data.get('token_address')}"
            for token_data in value
        ]
        self._watch_list = value
        self._manage_threads()

    @property
    def watch_list_keys(self):
        return self._watch_list_keys

    def _manage_threads(self):
        # stop threads that are not in watch_list
        logger.debug(self.scrappers.keys())
        marked_for_deletion = []
        for key in self.scrappers.keys():
            if key not in self.watch_list_keys:
                marked_for_deletion.append(key)
        for key in marked_for_deletion:
            self.scrappers.pop(key)  # delete is implemented as __del__ in ScraperThread
        # start threads that are in watch_list
        logger.debug(self.scrappers.keys())
        for token_data in self.watch_list:
            try:
                token_address = token_data.get("token_address")
                network = token_data.get("token_network")
            except KeyError as e:
                raise APIError("Token address or network not found") from e
            token_data.pop("token_network", None)  # Remove key if it exists
            token_data.pop("token_address", None)  # Remove key if it exists
            logger.debug(token_data)
            if f"{network}_{token_address}" not in self.scrappers.keys():
                self.scrappers[f"{network}_{token_address}"] = ScraperThread(
                    network, token_address, **token_data
                )
                self.scrappers[f"{network}_{token_address}"].start()
        logger.info("Threads updated")
        logger.info(f"current size: {len(self.scrappers)}")

    def start(self):
        while True:
            try:
                self.watch_list = self.scraper.get_watch_list_from_overkill().get(
                    "result", []
                )
                logger.info("Watch List Updated")
                time.sleep(60)
            except Exception as e:
                logger.error(f"Error in DexThreadManager: {e}")
                time.sleep(60)


class DexScraper:
    def __init__(self):
        self._name = "DegenAlphaRetriever"
        self._headers = {"Content-Type": "application/json"}
        self._network_ids_for_gecko_terminal = self.get_list_of_networks_from_gecko()
        # TODO: add round-robin proxy support

    # Getter for object's name haha
    @property
    def name(self):
        return self._name

    # Getter for the list of valid strings for network IDs for Gecko Terminal API
    @property
    def network_ids_for_gecko_terminal(self):
        return self._network_ids_for_gecko_terminal

    def search_pairs_from_dex_screener(self, token1: str, token2: str):
        url = "https://api.dexscreener.io/latest/dex/search?q={}%20{}"
        url = url.format(token1, token2)

        api_response = requests.get(url, headers=self._headers)
        api_data = api_response.json()

        # Retrieve the schema version and pairs array from the API response
        pairs = api_data.get("pairs", [])

        # Process the pairs array to access and modify token addresses as needed
        try:
            for pair in pairs:
                base_symbol = pair.get("baseToken")["symbol"]
                quote_symbol = pair.get("quoteToken")["symbol"]
                if base_symbol == token1 and quote_symbol == token2:
                    chain_id = pair.get("chainId")
                    price_usd = pair.get("priceUsd")
                    liq_usd = pair.get("liquidity")["usd"]
                    dex_id = pair.get("dexId")
                    dex_url = pair.get("url")
                    return (chain_id, price_usd, liq_usd, dex_id, dex_url)
        except KeyError as e:
            raise DexscreenerAPIError(
                "KeyError in search_pairs_from_dex_screener"
            ) from e

    def get_list_of_networks_from_gecko(self):
        url = "https://api.geckoterminal.com/api/v2/networks?page=1"
        network_ids = []

        api_response = requests.get(url, headers=self._headers)
        api_response.raise_for_status()
        api_data = api_response.json()
        try:
            data = api_data["data"]
            for element in data:
                network_ids.append(element["id"])
        except KeyError as e:
            raise GeckoTerminalAPIError(
                "KeyError in get_list_of_networks_from_gecko"
            ) from e

        return network_ids

    def get_token_price_from_gecko(self, network="solana", token_address=""):
        url = "https://api.geckoterminal.com/api/v2/simple/networks/{}/token_price/{}"
        url = url.format(network, token_address)

        api_response = requests.get(url, headers=self._headers)
        api_response.raise_for_status()
        api_data = api_response.json()
        try:
            data = api_data["data"]
            price = data["attributes"]["token_prices"][token_address]
        except KeyError as e:
            raise GeckoTerminalAPIError("KeyError in get_token_price_from_gecko") from e
        return price

    def get_pool_from_gecko(self, network="solana", pool_address=""):
        url = "https://api.geckoterminal.com/api/v2/networks/{}/pools/{}"
        url = url.format(network, pool_address)

        api_response = requests.get(url, headers=self._headers)
        api_response.raise_for_status()
        api_data = api_response.json()
        try:
            data = api_data["data"]
            price = data["attributes"]["base_token_price_usd"]
            name = data["attributes"]["name"]
            mc = data["attributes"]["market_cap_usd"]
            price_change = data["attributes"]["price_change_percentage"]
            txs = data["attributes"]["transactions"]
            vol = data["attributes"]["volume_usd"]
            fdv = data["attributes"]["fdv"]
            logger.info("Name: ", name, "\nPrice USD: ", price, "\nMarket Cap: ", mc)
        except KeyError as e:
            raise GeckoTerminalAPIError("KeyError in get_pool_from_gecko") from e
        return (name, price, mc)

    def get_top_pool_from_gecko(self, network="solana", token_address=""):
        url = "https://api.geckoterminal.com/api/v2/networks/{}/tokens/{}/pools?page=1"
        url = url.format(network, token_address)
        api_response = requests.get(url, headers=self._headers)
        api_response.raise_for_status()
        api_data = api_response.json()
        try:
            data = api_data["data"]
            top_pool = data[0]["attributes"]

            price = top_pool["base_token_price_usd"]
            pool_address = top_pool["address"]
            pool_name = top_pool["name"]
            mc = top_pool["market_cap_usd"]  # value / None
            price_change = top_pool["price_change_percentage"]  # m5, h1, h6, h24
            txs = top_pool["transactions"]  # m5, m15, m30, h1, h24
            vol = top_pool["volume_usd"]  # m5, h1, h6, h24
            fdv = top_pool["fdv_usd"]  # m5, h1, h6, h24
        except (KeyError, IndexError) as e:
            raise GeckoTerminalAPIError("Error in get_top_pool_from_gecko") from e
        return (pool_name, pool_address, price, mc, price_change, txs, vol)

    def get_ohlcv(
        self,
        network="solana",
        pool_address="",
        timeframe="minute",
        aggregate="1",
        limit="1",
        currency="usd",
    ):
        url = "https://api.geckoterminal.com/api/v2/networks/{}/pools/{}/ohlcv/{}?aggregate={}&limit={}&currency={}"
        url = url.format(network, pool_address, timeframe, aggregate, limit, currency)
        api_response = requests.get(url, headers=self._headers)
        api_response.raise_for_status()
        api_data = api_response.json()
        logger.debug(api_data)
        try:
            data = api_data["data"]
            tohlcv = data["attributes"]["ohlcv_list"][0]
            timestamp = tohlcv[0]
            open = tohlcv[1]
            high = tohlcv[2]
            low = tohlcv[3]
            close = tohlcv[4]
            volume = tohlcv[5]
        except (KeyError, IndexError) as e:
            raise GeckoTerminalAPIError("Error in get_ohlcv") from e
        return (timestamp, open, high, low, close, volume)

    def post_gecko_data_to_overkill(self, data: dict):
        url = "{}/v1/coin/gecko-terminal/data".format(APP_SETTINGS.overkill_api_url)

        headers = {
            "x-api-key": APP_SETTINGS.x_api_key,
            "x-api-secret": APP_SETTINGS.x_api_secret,
            **self._headers,
        }

        logger.debug(data)
        if not data:
            raise APIError("Data is empty")

        api_response = requests.post(url, headers=headers, json=data)
        api_response.raise_for_status()
        return api_response.json()

    def get_gecko_data_from_overkill(self, params: dict):
        url = "{}/v1/coin/gecko-terminal/data".format(APP_SETTINGS.overkill_api_url)

        headers = {
            "x-api-key": APP_SETTINGS.x_api_key,
            "x-api-secret": APP_SETTINGS.x_api_secret,
            **self._headers,
        }

        if not params:
            raise APIError("Params is empty")

        api_response = requests.get(url, headers=headers, params={**params})
        api_response.raise_for_status()
        return api_response.json()

    def get_watch_list_from_overkill(self):
        url = "{}/v1/coin/watch".format(APP_SETTINGS.overkill_api_url)

        headers = {
            "x-api-key": APP_SETTINGS.x_api_key,
            "x-api-secret": APP_SETTINGS.x_api_secret,
            **self._headers,
        }
        api_response = requests.get(url, headers=headers)
        api_response.raise_for_status()
        return api_response.json()

    def delete_coin_from_watch_list(self, token_data: dict):
        url = "{}/v1/coin/watch".format(APP_SETTINGS.overkill_api_url)

        headers = {
            "x-api-key": APP_SETTINGS.x_api_key,
            "x-api-secret": APP_SETTINGS.x_api_secret,
            **self._headers,
        }
        if not (token_data):
            raise APIError("No token information to delete")
        logger.info(token_data)
        try:
            payload = {
                "token_name": token_data["token_name"],
                "token_ticker": token_data["token_ticker"],
                "comment": token_data.get("comment", ""),
            }
        except KeyError as e:
            raise APIError("Token name or ticker not found") from e
        api_response = requests.delete(url, headers=headers, json=payload)
        api_response.raise_for_status()
        return api_response.json()
