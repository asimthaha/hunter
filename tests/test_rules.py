import pytest

from src import config, rules

PROFILE = {"notice_days": 30, "min_pay": {"india": {"amount": 2500000, "currency": "INR"},
                                           "remote-foreign": {"amount": 40000, "currency": "USD"}}}
PREFS = {**config.DEFAULT_PREFS, "title_exclude_any": ["sales"]}


# ---- scams
@pytest.mark.parametrize("text", [
    "A refundable security deposit is required before joining.",
    "Pay a registration fee of Rs 2000 to confirm your slot.",
    "We will send an equipment cheque for your laptop purchase.",
    "Training fee applies for all new hires.",
])
def test_money_requests_rejected(text):
    flags = rules.scan_scam(text)
    assert any(f.kind == "asks_for_money" and f.action == "reject" for f in flags)


def test_negated_money_mention_not_rejected():
    assert not any(f.kind == "asks_for_money" for f in rules.scan_scam("We never charge a registration fee."))


@pytest.mark.parametrize("text,kind", [
    ("Interview will be on Telegram.", "off_platform_contact"),
    ("Send your Aadhaar and bank details to proceed.", "id_request"),
    ("Contact hr.acme@gmail.com", "free_email_recruiter"),
])
def test_other_scam_patterns(text, kind):
    assert any(f.kind == kind for f in rules.scan_scam(text, "Acme"))


def test_pan_india_is_not_an_id_request():
    assert not rules.scan_scam("Opportunities across PAN India offices.")


def test_matching_company_email_not_flagged():
    assert rules.scan_scam("Email careers@acme.com", "Acme") == []


# ---- location
def test_us_or_india_passes():
    assert rules.check_location("US or India") is None
    assert rules.check_location("Remote", "US only (India also considered)") is None


def test_us_only_rejected():
    assert rules.check_location("Remote - US only") is not None


# ---- title
def test_title_rules():
    assert rules.check_title("Senior Sales Engineer", PREFS) is not None
    assert rules.check_title("Backend Engineer", PREFS) is None
    assert rules.check_title("Software Engineering Intern", PREFS) is not None


# ---- notice
def test_notice_incompatible():
    res = rules.apply_rules({"title": "Engineer", "description": "Immediate joiners only."}, PREFS, PROFILE)
    assert res.decision == "reject" and "notice_incompatible" in res.reason


def test_notice_ok_when_long_enough_or_unspecified():
    assert rules.required_notice_days("Notice period up to 60 days") == (60, rules.required_notice_days("Notice period up to 60 days")[1])
    assert rules.check_notice(60, PROFILE) is None
    assert rules.check_notice(None, PROFILE) is None
    assert rules.check_notice(15, PROFILE) is not None


def test_immediate_joiners_preferred_is_not_hard():
    assert rules.required_notice_days("Immediate joiners preferred.") is None


# ---- pay
def pay(amount_min, amount_max, cur, period, stated=True):
    return {"stated": stated, "amount_min": amount_min, "amount_max": amount_max, "currency": cur, "period": period}


def test_pay_not_stated_is_not_below_min():
    assert rules.pay_status(pay(None, None, None, "not_stated", False), "india", PROFILE) == "not_stated"


def test_lpa_vs_floor():
    assert rules.pay_status(pay(20, 22, "INR", "lakh_per_annum"), "india", PROFILE) == "below_min"
    assert rules.pay_status(pay(25, 30, "INR", "lakh_per_annum"), "india", PROFILE) == "ok"


def test_range_uses_top_and_monthly_is_annualised():
    assert rules.pay_status(pay(150000, 250000, "INR", "month"), "india", PROFILE) == "ok"      # 3M/yr
    assert rules.pay_status(pay(100000, 150000, "INR", "month"), "india", PROFILE) == "below_min"  # 1.8M


def test_cross_currency():
    assert rules.pay_status(pay(30000, 35000, "USD", "year"), "remote-foreign", PROFILE) == "below_min"
    assert rules.pay_status(pay(50, 60, "USD", "hour"), "remote-foreign", PROFILE) == "ok"
    assert rules.pay_status(pay(5000, 6000, "USD", "month"), "india", PROFILE) == "ok"          # 72k USD ≈ 6M INR


def test_unknown_when_no_floor_for_bucket():
    assert rules.pay_status(pay(1, 2, "USD", "year"), "relocation", PROFILE) == "unknown"


def test_suspicious_pay_flagged():
    assert rules.pay_suspiciously_high(pay(900000, 900000, "USD", "year"), PREFS) is not None
    assert rules.pay_suspiciously_high(pay(90000, 90000, "USD", "year"), PREFS) is None
