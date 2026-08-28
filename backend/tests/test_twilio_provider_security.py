"""TwiML output must treat tenant-controlled values as data, never markup."""

from xml.etree import ElementTree

from app.telephony.twilio_provider import TwilioProvider


def _xml(value: str) -> ElementTree.Element:
    return ElementTree.fromstring(value)


def test_greeting_escapes_message_markup():
    message = "Sales & support </Say><Dial>+15551234567</Dial><Say>"
    root = _xml(TwilioProvider().generate_greeting(message, "alice").xml)

    assert [child.tag for child in root] == ["Say"]
    assert root.find("Say").text == message
    assert root.find("Dial") is None


def test_twiml_helpers_escape_attributes_and_nested_values():
    provider = TwilioProvider()

    gather_root = _xml(
        provider.generate_gather(
            "Choose sales & support",
            "alice",
            "https://voice.example.com/gather?team=sales&region=uae",
        ).xml
    )
    gather = gather_root.find("Gather")
    assert gather is not None
    assert gather.attrib["action"] == ("https://voice.example.com/gather?team=sales&region=uae")
    assert gather.find("Say").text == "Choose sales & support"

    stream_root = _xml(
        provider.generate_connect_stream("wss://voice.example.com/live?agent=a&tenant=b").xml
    )
    assert stream_root.find("./Connect/Stream").attrib["url"] == (
        "wss://voice.example.com/live?agent=a&tenant=b"
    )

    number = "+15551234567</Number><Sip>attacker@example.com</Sip><Number>"
    transfer_root = _xml(provider.generate_transfer(number, "+15557654321").xml)
    assert transfer_root.find("./Dial/Number").text == number
    assert transfer_root.find(".//Sip") is None
