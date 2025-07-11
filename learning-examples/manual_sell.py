import asyncio
import os
import struct

import base58
from construct import Bytes, Flag, Int64ul, Struct
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import get_associated_token_address

# Here and later all the discriminators are precalculated. See learning-examples/calculate_discriminator.py
EXPECTED_DISCRIMINATOR = struct.pack("<Q", 6966180631402821399)
TOKEN_DECIMALS = 6

# Global constants
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_GLOBAL = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
PUMP_EVENT_AUTHORITY = Pubkey.from_string(
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"
)
PUMP_FEE = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
SYSTEM_RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
SOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
LAMPORTS_PER_SOL = 1_000_000_000
UNIT_PRICE = 10_000_000
UNIT_BUDGET = 100_000

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


class BondingCurveState:
    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
        "creator" / Bytes(32),  # Added new creator field - 32 bytes for Pubkey
    )

    def __init__(self, data: bytes) -> None:
        """Parse bonding curve data."""
        if data[:8] != EXPECTED_DISCRIMINATOR:
            raise ValueError("Invalid curve state discriminator")

        parsed = self._STRUCT.parse(data[8:])
        self.__dict__.update(parsed)

        # Convert raw bytes to Pubkey for creator field
        if hasattr(self, 'creator') and isinstance(self.creator, bytes):
            self.creator = Pubkey.from_bytes(self.creator)


async def get_pump_curve_state(
    conn: AsyncClient, curve_address: Pubkey
) -> BondingCurveState:
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError("Invalid curve state discriminator")

    return BondingCurveState(data)


def get_bonding_curve_address(mint: Pubkey) -> tuple[Pubkey, int]:
    return Pubkey.find_program_address([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)


def find_associated_bonding_curve(mint: Pubkey, bonding_curve: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [
            bytes(bonding_curve),
            bytes(SYSTEM_TOKEN_PROGRAM),
            bytes(mint),
        ],
        SYSTEM_ASSOCIATED_TOKEN_ACCOUNT_PROGRAM,
    )
    return derived_address


def find_creator_vault(creator: Pubkey) -> Pubkey:
    derived_address, _ = Pubkey.find_program_address(
        [
            b"creator-vault",
            bytes(creator)
        ],
        PUMP_PROGRAM,
    )
    return derived_address


def calculate_pump_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


async def get_token_balance(conn: AsyncClient, associated_token_account: Pubkey):
    response = await conn.get_token_account_balance(associated_token_account)
    if response.value:
        return int(response.value.amount)
    return 0


async def sell_token(
    mint: Pubkey,
    bonding_curve: Pubkey,
    associated_bonding_curve: Pubkey,
    creator_vault: Pubkey,
    slippage: float = 0.25,
    max_retries=5,
):
    private_key = base58.b58decode(os.environ.get("SOLANA_PRIVATE_KEY"))
    payer = Keypair.from_bytes(private_key)

    async with AsyncClient(RPC_ENDPOINT) as client:
        associated_token_account = get_associated_token_address(payer.pubkey(), mint)

        # Get token balance
        token_balance = await get_token_balance(client, associated_token_account)
        token_balance_decimal = token_balance / 10**TOKEN_DECIMALS
        print(f"Token balance: {token_balance_decimal}")
        if token_balance == 0:
            print("No tokens to sell.")
            return

        # Fetch the token price
        curve_state = await get_pump_curve_state(client, bonding_curve)
        token_price_sol = calculate_pump_curve_price(curve_state)
        print(f"Price per Token: {token_price_sol:.20f} SOL")

        # Calculate minimum SOL output
        amount = token_balance
        min_sol_output = float(token_balance_decimal) * float(token_price_sol)
        slippage_factor = 1 - slippage
        min_sol_output = int((min_sol_output * slippage_factor) * LAMPORTS_PER_SOL)

        print(f"Selling {token_balance_decimal} tokens")
        print(f"Minimum SOL output: {min_sol_output / LAMPORTS_PER_SOL:.10f} SOL")

        accounts = [
            AccountMeta(pubkey=PUMP_GLOBAL, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PUMP_FEE, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=bonding_curve, is_signer=False, is_writable=True
            ),
            AccountMeta(
                pubkey=associated_bonding_curve,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=associated_token_account,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=payer.pubkey(), is_signer=True, is_writable=True
            ),
            AccountMeta(
                pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=creator_vault,
                is_signer=False,
                is_writable=True,
            ),
            AccountMeta(
                pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PUMP_EVENT_AUTHORITY, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=PUMP_PROGRAM, is_signer=False, is_writable=False
            ),
        ]

        discriminator = struct.pack("<Q", 12502976635542562355)
        data = (
            discriminator
            + struct.pack("<Q", amount)
            + struct.pack("<Q", min_sol_output)
        )
        sell_ix = Instruction(PUMP_PROGRAM, data, accounts)

        msg = Message([set_compute_unit_price(1_000), sell_ix], payer.pubkey())
        recent_blockhash = await client.get_latest_blockhash()
        opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
        # Continue with the sell transaction
        for attempt in range(max_retries):
            try:
                tx = await client.send_transaction(
                    Transaction(
                        [payer],
                        msg,
                        recent_blockhash.value.blockhash,
                    ),
                    opts=opts,
                )
                tx_hash = tx.value
                print(f"Transaction sent: https://explorer.solana.com/tx/{tx_hash}")
                await client.confirm_transaction(tx_hash, commitment="confirmed", sleep_seconds=1)
                print("Transaction confirmed")
                return  # Success, exit the function
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e!s}")
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Exponential backoff
                    print(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Unable to complete the transaction.")


async def main():
    # Replace these with the actual values for the token you want to sell
    mint = Pubkey.from_string("...")
    bonding_curve, _ = get_bonding_curve_address(mint)
    associated_bonding_curve = find_associated_bonding_curve(mint, bonding_curve)

    async with AsyncClient(RPC_ENDPOINT) as client:
        curve_state = await get_pump_curve_state(client, bonding_curve)

    creator_vault = find_creator_vault(curve_state.creator)

    slippage = 0.25  # 25% slippage tolerance

    print(f"Bonding curve address: {bonding_curve}")
    print(f"Selling tokens with {slippage * 100:.1f}% slippage tolerance...")
    await sell_token(mint, bonding_curve, associated_bonding_curve, creator_vault, slippage)


if __name__ == "__main__":
    asyncio.run(main())
