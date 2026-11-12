"""
Personal data: checksums, detection, masking, restoring, and the vault.

Run the Redis vault tests against a real Redis (database 15 is wiped):

    TEST_REDIS_URL=redis://localhost:6379/15 pytest tests/test_guardrails_pii.py
"""
from __future__ import annotations

import pytest

from app.agents_platform.guardrails.pii import validators
from app.agents_platform.guardrails.pii.detectors import POLICY, Action, PiiClass, detect
from app.agents_platform.guardrails.pii.masker import mask, redact_leaks, restore
from app.agents_platform.guardrails.pii.vault import InMemoryVault, RedisVault, generate_key, parse_key
from app.agents_platform.infra.redis import RedisProvider
from tests.redis_helpers import fresh_provider, needs_redis


def aadhaar(prefix: str = "23456789012") -> str:
    return prefix + validators.verhoeff_check_digit(prefix)


def classes(text: str) -> list[PiiClass]:
    return [d.cls for d in detect(text)]


# --- validators -----------------------------------------------------------------------------------


@pytest.mark.parametrize("number,ok", [("4111111111111111", True), ("4111 1111 1111 1112", False), ("5500005555555559", True)])
def test_luhn(number, ok):
    assert validators.luhn_valid(number) is ok


def test_luhn_rejects_the_wrong_length():
    assert validators.luhn_valid("424242") is False


def test_verhoeff_round_trips_a_generated_check_digit():
    for prefix in ("23456789012", "98765432101", "50505050505"):
        assert validators.verhoeff_valid(prefix + validators.verhoeff_check_digit(prefix))


def test_verhoeff_catches_a_single_digit_error_and_a_swap():
    good = aadhaar()
    wrong = good[:-1] + str((int(good[-1]) + 1) % 10)
    swapped = good[0] + good[2] + good[1] + good[3:]
    assert not validators.verhoeff_valid(wrong)
    assert not validators.verhoeff_valid(swapped)


def test_aadhaar_never_starts_with_zero_or_one():
    assert validators.aadhaar_valid(aadhaar("23456789012"))
    assert not validators.aadhaar_valid("0" + aadhaar()[1:])
    assert not validators.aadhaar_valid("1" + aadhaar()[1:])


def test_pan_needs_a_real_holder_type_letter():
    assert validators.pan_valid("ABCPE1234F") and not validators.pan_valid("ABCZE1234F")


def test_ifsc_and_gps_ranges():
    assert validators.ifsc_valid("SBIN0001234") and not validators.ifsc_valid("SBIN1001234")
    assert validators.gps_valid(19.076, 72.8777) and not validators.gps_valid(120.0, 72.8)


# --- detection ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("mail kp@example.com please", PiiClass.EMAIL),
        ("call +91 98765 43210", PiiClass.PHONE),
        ("call 9876543210", PiiClass.PHONE),
        (f"aadhaar {aadhaar()[:4]} {aadhaar()[4:8]} {aadhaar()[8:]}", PiiClass.AADHAAR),
        ("pan ABCPE1234F", PiiClass.PAN),
        ("ifsc SBIN0001234", PiiClass.IFSC),
        ("at 19.0760, 72.8777", PiiClass.GPS),
        ("consumer no 170012345678", PiiClass.CONSUMER_NO),
        ("Consumer Number: 1700-1234-5678", PiiClass.CONSUMER_NO),
        ("meter number MT123456", PiiClass.METER_NO),
        ("card 4111 1111 1111 1111", PiiClass.CARD),
        ("pay ravi@okaxis", PiiClass.UPI),
        ("a/c no 123456789012", PiiClass.BANK_ACCOUNT),
        ("my password is hunter2xyz", PiiClass.SECRET),
        ("key sk-or-v1-0123456789abcdef0123456789abcdef", PiiClass.SECRET),
        ("token ghp_" + "a" * 36, PiiClass.SECRET),
        ("otp is 482913", PiiClass.SECRET),
    ],
)
def test_each_class_is_detected(text, expected):
    assert classes(text) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "Why is the battery at 62%?",
        "I exported 9876.5 kWh this year",
        "total import 1234 5678 9012 kWh",  # right shape for Aadhaar, wrong checksum
        "reading 4111 1111 1111 1112",  # right shape for a card, wrong checksum
        "price is 4.5 rupees at 19.07",  # one coordinate, too few decimals
        "consumer number is wrong again",  # a label with no digits
        "meter number is incorrect",  # a label with a word, not an id
        "fault code E04 on the 5kW inverter",
        "call 1800 123 4567 for support",  # toll-free is not a mobile number
    ],
)
def test_look_alike_numbers_and_labels_are_not_personal_data(text):
    assert detect(text) == []


def test_an_identifier_is_only_recognised_after_its_label():
    assert detect("170012345678") == []
    assert classes("consumer no 170012345678") == [PiiClass.CONSUMER_NO]


def test_a_labelled_consumer_number_wins_over_a_phone_or_aadhaar_reading_of_the_same_digits():
    # 12 digits that would also pass as a mobile or Aadhaar must stay the more specific, labelled class
    assert classes(f"consumer no {aadhaar()}") == [PiiClass.CONSUMER_NO]
    assert classes("consumer number 9876543210") == [PiiClass.CONSUMER_NO]


def test_a_upi_id_is_not_mistaken_for_an_email():
    assert classes("send to name@okhdfcbank") == [PiiClass.UPI]
    assert classes("send to name@gmail.com") == [PiiClass.EMAIL]


def test_spans_never_overlap_and_come_in_reading_order():
    found = detect("mail a@b.com, call 9876543210, consumer no 170012345678, pan ABCPE1234F")
    assert [d.cls for d in found] == [PiiClass.EMAIL, PiiClass.PHONE, PiiClass.CONSUMER_NO, PiiClass.PAN]
    assert all(a.end <= b.start for a, b in zip(found, found[1:]))


def test_the_policy_table_blocks_what_has_no_place_in_the_chat():
    blocked = {c for c, p in POLICY.items() if p.action is Action.BLOCK}
    assert blocked == {PiiClass.CARD, PiiClass.UPI, PiiClass.BANK_ACCOUNT, PiiClass.SECRET}
    shown = {c for c, p in POLICY.items() if p.show_in_answer}
    assert shown == {PiiClass.CONSUMER_NO, PiiClass.METER_NO}


# --- masking and restoring ------------------------------------------------------------------------


def test_masking_replaces_values_with_typed_tokens():
    result = mask("mail kp@example.com or ravi@example.org, call 9876543210")
    assert result.text == "mail <EMAIL_1> or <EMAIL_2>, call <PHONE_1>"
    assert result.tokens == {
        "<EMAIL_1>": "kp@example.com",
        "<EMAIL_2>": "ravi@example.org",
        "<PHONE_1>": "9876543210",
    }
    assert result.counts == {"EMAIL": 2, "PHONE": 1}


def test_the_same_value_gets_the_same_token():
    result = mask("kp@example.com again kp@example.com")
    assert result.text == "<EMAIL_1> again <EMAIL_1>" and len(result.tokens) == 1


def test_a_blocked_class_is_removed_and_never_tokenised():
    result = mask("card 4111 1111 1111 1111 ok")
    assert result.text == "card [removed] ok"
    assert result.tokens == {} and result.blocked == (PiiClass.CARD,)


def test_text_without_personal_data_is_returned_as_is():
    result = mask("why is the battery low?")
    assert result.text == "why is the battery low?" and result.tokens == {} and result.counts == {}


def test_the_raw_values_are_not_in_the_masked_results_repr():
    assert "kp@example.com" not in repr(mask("mail kp@example.com"))


def test_restore_returns_only_the_classes_that_may_be_shown():
    tokens = {"<CONSUMER_NO_1>": "170012345678", "<EMAIL_1>": "kp@example.com"}
    text = restore("Bill for <CONSUMER_NO_1>, sent to <EMAIL_1>.", tokens)
    assert text == "Bill for 170012345678, sent to [hidden email]."


def test_restore_hides_a_token_the_model_invented_or_took_from_another_request():
    tokens = {"<CONSUMER_NO_1>": "170012345678"}
    assert restore("see <CONSUMER_NO_2> and <PHONE_1>", tokens) == "see [hidden consumer number] and [hidden phone number]"


def test_restore_leaves_unrelated_angle_brackets_alone():
    assert restore("use <b>bold</b> and <FOO_1>", {}) == "use <b>bold</b> and <FOO_1>"


def test_a_round_trip_returns_exactly_what_the_user_typed_for_shown_classes():
    original = "my consumer no 170012345678, why is the bill high?"
    masked = mask(original)
    assert "170012345678" not in masked.text
    assert restore(masked.text, masked.tokens) == original


def test_redact_leaks_hides_what_the_user_must_not_be_shown():
    text, found = redact_leaks("Call 9876543210 or mail help@brand.com; consumer no 170012345678")
    assert text == "Call [hidden phone number] or mail [hidden email]; consumer no 170012345678"
    assert dict(found) == {"PHONE": 1, "EMAIL": 1}


def test_redact_leaks_is_a_no_op_on_a_clean_answer():
    text, found = redact_leaks("Solar covers the load and the battery is at 62%.")
    assert text == "Solar covers the load and the battery is at 62%." and not found


# --- vault: memory --------------------------------------------------------------------------------


async def test_the_memory_vault_stores_loads_and_discards():
    vault = InMemoryVault()
    assert await vault.store("r1", {"<EMAIL_1>": "kp@example.com"})
    assert await vault.load("r1") == {"<EMAIL_1>": "kp@example.com"}
    assert await vault.load("r2") == {}
    await vault.discard("r1")
    assert await vault.load("r1") == {}


async def test_the_memory_vault_forgets_after_its_time_limit():
    now = [100.0]
    vault = InMemoryVault(ttl_seconds=60, clock=lambda: now[0])
    await vault.store("r1", {"<EMAIL_1>": "kp@example.com"})
    now[0] = 159.0
    assert await vault.load("r1")
    now[0] = 161.0
    assert await vault.load("r1") == {}


async def test_the_memory_vault_has_a_hard_ceiling():
    vault = InMemoryVault(max_requests=3)
    for i in range(10):
        await vault.store(f"r{i}", {"<EMAIL_1>": "x@y.com"})
    assert len(vault._items) <= 3
    assert await vault.load("r9")  # the newest survives, the oldest are dropped


def test_vault_keys_must_be_exactly_32_bytes():
    assert len(parse_key(generate_key())) == 32
    with pytest.raises(ValueError):
        parse_key("c2hvcnQ=")


# --- vault: redis ---------------------------------------------------------------------------------

@needs_redis
async def test_the_redis_vault_round_trips_and_stores_ciphertext_only():
    async with fresh_provider() as provider:
        vault = RedisVault(provider, key=parse_key(generate_key()), ttl_seconds=60)
        await vault.store("req-1", {"<EMAIL_1>": "kp@example.com", "<PHONE_1>": "9876543210"})

        assert await vault.load("req-1") == {"<EMAIL_1>": "kp@example.com", "<PHONE_1>": "9876543210"}
        client = await provider.client()
        raw = await client.hgetall("volta:ai:pii:req-1")
        assert raw and all("kp@example.com" not in v and "9876543210" not in v for v in raw.values())
        assert 0 < await client.ttl("volta:ai:pii:req-1") <= 60


@needs_redis
async def test_the_redis_vault_refuses_a_value_moved_to_another_request_or_token():
    async with fresh_provider() as provider:
        vault = RedisVault(provider, key=parse_key(generate_key()))
        await vault.store("req-1", {"<EMAIL_1>": "kp@example.com"})
        client = await provider.client()
        blob = (await client.hgetall("volta:ai:pii:req-1"))["<EMAIL_1>"]

        await client.hset("volta:ai:pii:req-2", "<EMAIL_1>", blob)  # same token, other request
        await client.hset("volta:ai:pii:req-1", "<PHONE_1>", blob)  # same request, other token
        assert await vault.load("req-2") == {}
        assert await vault.load("req-1") == {"<EMAIL_1>": "kp@example.com"}


@needs_redis
async def test_a_different_key_cannot_read_the_vault():
    async with fresh_provider() as provider:
        await RedisVault(provider, key=parse_key(generate_key())).store("req-1", {"<EMAIL_1>": "kp@example.com"})
        assert await RedisVault(provider, key=parse_key(generate_key())).load("req-1") == {}


@needs_redis
async def test_discard_removes_the_entry():
    async with fresh_provider() as provider:
        vault = RedisVault(provider, key=parse_key(generate_key()))
        await vault.store("req-1", {"<EMAIL_1>": "kp@example.com"})
        await vault.discard("req-1")
        assert await vault.load("req-1") == {}


async def test_the_redis_vault_fails_open_when_redis_is_down():
    down = RedisProvider("redis://127.0.0.1:1/0", cooldown_seconds=30.0)
    vault = RedisVault(down, key=parse_key(generate_key()))
    assert await vault.store("req-1", {"<EMAIL_1>": "kp@example.com"}) is False
    assert await vault.load("req-1") == {}
    await vault.discard("req-1")  # must not raise
