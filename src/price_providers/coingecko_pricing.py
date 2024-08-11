import os
import time
from typing import Dict, List, Optional
import requests
import json
from web3 import Web3
from src.helpers.config import logger, get_web3_instance
from src.helpers.helper_functions import get_finalized_block_number
from src.constants import (
    NATIVE_ETH_TOKEN_ADDRESS,
    WETH_TOKEN_ADDRESS,
    TOKEN_LIST_RELOAD_TIME,
    COINGECKO_TIME_LIMIT,
    BUFFER_TIME,
)

coingecko_api_key = os.getenv("COINGECKO_API_KEY")


class CoingeckoPriceProvider:
    """
    Purpose of this class is to fetch historical token prices from Coingecko.
    """

    def __init__(self) -> None:
        self.web3 = get_web3_instance()
        self.filtered_token_list = self.fetch_coingecko_list()
        self.last_reload_time = time.time()  # current time in seconds since epoch

    def fetch_coingecko_list(self) -> List[Dict]:
        """
        Fetch and filter the list of tokens (currently filters only Ethereum)
        from the Coingecko API.
        """
        url = (
            f"https://pro-api.coingecko.com/api/v3/coins/"
            f"list?include_platform=true&status=active"
        )
        headers = {
            "accept": "application/json",
        }
        if coingecko_api_key:
            headers["x-cg-pro-api-key"] = coingecko_api_key

        response = requests.get(url, headers=headers)
        tokens_list = json.loads(response.text)
        return [
            {"id": item["id"], "platforms": {"ethereum": item["platforms"]["ethereum"]}}
            for item in tokens_list
            if "ethereum" in item["platforms"]
        ]

    def check_reload_token_list(self) -> bool:
        """check if the token list needs to be reloaded based on time."""
        current_time = time.time()
        elapsed_time = current_time - self.last_reload_time
        # checks for set elapsed time (currently 24 hours), in seconds
        return elapsed_time >= TOKEN_LIST_RELOAD_TIME

    def get_token_id_by_address(self, token_address) -> Optional[str]:
        """
        Check to see if an updated token list is required.
        Get the token ID by searching for the token address.
        """
        if self.check_reload_token_list():
            self.filtered_token_list = self.fetch_coingecko_list()
            self.last_reload_time = (
                time.time()
            )  # update the last reload time to current time
        for token in self.filtered_token_list:
            if token["platforms"].get("ethereum") == token_address:
                return token["id"]
        return None

    def fetch_api_price(
        self, token_id: str, start_timestamp: int, end_timestamp: int
    ) -> Optional[float]:
        """
        Makes call to Coingecko API to fetch price, between a start and end timestamp.
        """
        if not coingecko_api_key:
            logger.warning("Coingecko API key is not set.")
            return None
        # price of token is returned in ETH
        url = (
            f"https://pro-api.coingecko.com/api/v3/coins/{token_id}/market_chart/range"
            f"?vs_currency=eth&from={start_timestamp}&to={end_timestamp}"
        )
        headers = {
            "accept": "application/json",
            "x-cg-pro-api-key": coingecko_api_key,
        }
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            # return available coingecko price, which is the closest to the block timestamp
            if len(data["prices"]) != 0:
                price = data["prices"][0][1]
                return price
            return None
        except requests.RequestException as e:
            logger.warning(f"Error fetching price from Coingecko API: {e}")
            return None

    def price_not_retrievable(self, block_start_timestamp: int) -> bool:
        """
        This function checks if the time elapsed between the latest block and block being processed
        is less than 2 days, which is coingecko's time frame for fetching 5-minutely data.
        """
        newest_block_timestamp = self.web3.eth.get_block(
            get_finalized_block_number(self.web3)
        )["timestamp"]
        return (newest_block_timestamp - block_start_timestamp) > COINGECKO_TIME_LIMIT

    def get_price(self, block_number: int, token_address: str) -> Optional[float]:
        """
        Function returns coingecko price for a token address,
        closest to and at least as large as the block timestamp for a given tx hash.
        """

        block_start_timestamp = self.web3.eth.get_block(block_number)["timestamp"]
        if self.price_not_retrievable(block_start_timestamp):
            return None

        # Coingecko doesn't store ETH address, which occurs commonly in imbalances.
        # Approximate WETH price as equal to ETH.
        if Web3.to_checksum_address(token_address) in (
            NATIVE_ETH_TOKEN_ADDRESS,
            WETH_TOKEN_ADDRESS,
        ):
            return 1.0

        # We need to provide a sufficient buffer time for fetching 5-minutely prices from coingecko.
        # If too short, it's possible that no price may be returned. We use the first value returned,
        # which would be closest to block timestamp
        block_end_timestamp = block_start_timestamp + BUFFER_TIME

        # Coingecko requires a lowercase token address
        token_address = token_address.lower()
        token_id = self.get_token_id_by_address(token_address)
        if not token_id:
            logger.warning(
                f"Token ID not found for the given address on Coingecko: {token_address}"
            )
            return None
        try:
            api_price = self.fetch_api_price(
                token_id, block_start_timestamp, block_end_timestamp
            )
            if api_price is None:
                logger.warning(f"API returned None for token ID: {token_id}")
                return None
        except requests.RequestException as e:
            logger.error(f"Error fetching price from API: {e}")
            return None

        return api_price
