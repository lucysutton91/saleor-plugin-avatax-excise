from decimal import Decimal
from json import JSONDecodeError
from unittest.mock import Mock, patch
from urllib.parse import urljoin

import pytest
from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.db import connection
from django.test import override_settings
from prices import Money, TaxedMoney
from requests import RequestException
from dataclasses import asdict

from saleor.account.models import Address
from saleor.checkout.models import CheckoutLine, Checkout
from saleor.checkout.utils import add_variant_to_checkout
from ..compat import fetch_checkout_lines
from saleor.core.prices import quantize_price
from saleor.core.taxes import TaxError
from saleor.product.models import ProductType, ProductVariant
from saleor.warehouse.models import Warehouse
from ....manager import get_plugins_manager
from ....models import PluginConfiguration
from ... import AvataxConfiguration
from ..utils import api_post_request, get_metadata_key, get_api_url, get_order_request_data
from ..plugin import AvataxExcisePlugin


@pytest.fixture
def plugin_configuration(db):
    def set_configuration(
        username="test", password="test", sandbox=True, company_id="test",
    ):
        data = {
            "active": True,
            "name": AvataxExcisePlugin.PLUGIN_NAME,
            "configuration": [
                {"name": "Username or account", "value": "test"},
                {"name": "Password or license", "value": "test"},
                {"name": "Use sandbox", "value": True},
                {"name": "Company name", "value": "test"},
                {"name": "Autocommit", "value": False},
            ],
        }
        configuration = PluginConfiguration.objects.create(
            identifier=AvataxExcisePlugin.PLUGIN_ID, **data
        )
        return configuration

    return set_configuration


@pytest.fixture
def address_usa_tx():
    return Address.objects.create(
        first_name="John",
        last_name="Doe",
        street_address_1="1100 Congress Ave",
        city="Austin",
        postal_code="78701",
        country_area="TX",
        country="US",
        phone="",
    )


@pytest.fixture
def cigar_product_type():
    return ProductType.objects.create(
        name="Cigar",
        private_metadata={
            "mirumee.taxes.avalara_excise:UnitOfMeasure": "PAC",
            "mirumee.taxes.avalara_excise:UnitQuantityUnitOfMeasure": "EA",
        },
    )


@pytest.fixture
def warehouse(address_usa_tx, shipping_zone):
    warehouse = Warehouse.objects.create(
        address=address_usa_tx,
        name="Example Warehouse",
        slug="example-warehouse",
        email="test@example.com",
    )
    warehouse.shipping_zones.add(shipping_zone)
    warehouse.save()
    return warehouse


@pytest.fixture
def reset_sequences():
    sequence_sql = connection.ops.sequence_reset_sql(
        no_style(), [CheckoutLine])
    with connection.cursor() as cursor:
        for sql in sequence_sql:
            cursor.execute(sql)


@patch("saleor.plugins.avatax.excise.plugin.api_get_request")
def test_save_plugin_configuration(api_get_request_mock, settings):
    settings.PLUGINS = [
        "saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"]
    api_get_request_mock.return_value = {"authenticated": True}
    manager = get_plugins_manager()
    manager.save_plugin_configuration(
        AvataxExcisePlugin.PLUGIN_ID,
        {
            "active": True,
            "configuration": [
                {"name": "Username or account", "value": "test"},
                {"name": "Password or license", "value": "test"},
            ],
        },
    )
    manager.save_plugin_configuration(
        AvataxExcisePlugin.PLUGIN_ID, {"active": True})
    plugin_configuration = PluginConfiguration.objects.get(
        identifier=AvataxExcisePlugin.PLUGIN_ID
    )
    assert plugin_configuration.active


def test_save_plugin_configuration_invalid(settings):
    settings.PLUGINS = [
        "saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"]
    manager = get_plugins_manager()
    with pytest.raises(ValidationError):
        manager.save_plugin_configuration(
            AvataxExcisePlugin.PLUGIN_ID,
            {
                "active": True,
                "configuration": [
                    {"name": "Username or account", "value": ""},
                    {"name": "Password or license", "value": ""},
                ],
            },
        )
    plugin_configuration = PluginConfiguration.objects.get(
        identifier=AvataxExcisePlugin.PLUGIN_ID
    )
    assert not plugin_configuration.active


@patch("saleor.plugins.avatax.excise.plugin.api_get_request")
def test_save_plugin_configuration_authentication_failed(
    api_get_request_mock, settings
):
    settings.PLUGINS = [
        "saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"]
    api_get_request_mock.return_value = {"authenticated": False}
    manager = get_plugins_manager()

    with pytest.raises(ValidationError) as e:
        manager.save_plugin_configuration(
            AvataxExcisePlugin.PLUGIN_ID,
            {
                "active": True,
                "configuration": [
                    {"name": "Username or account", "value": "test"},
                    {"name": "Password or license", "value": "test"},
                ],
            },
        )
    assert e._excinfo[1].args[0] == "Authentication failed. Please check provided data."
    plugin_configuration = PluginConfiguration.objects.get(
        identifier=AvataxExcisePlugin.PLUGIN_ID
    )
    assert not plugin_configuration.active


@pytest.mark.vcr
@pytest.mark.parametrize(
    "with_discount, expected_net, expected_gross, taxes_in_prices",
    [(False, "30.00", "32.29", True), ],
)
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_calculate_checkout_line_total(
    reset_sequences,  # pylint: disable=unused-argument
    with_discount,
    expected_net,
    expected_gross,
    taxes_in_prices,
    discount_info,
    checkout_with_item,
    address_usa_tx,
    address_usa,
    site_settings,
    shipping_zone,
    plugin_configuration,
):
    plugin_configuration()
    manager = get_plugins_manager()

    checkout_with_item.shipping_address = address_usa_tx
    checkout_with_item.shipping_method = shipping_zone.shipping_methods.get()
    checkout_with_item.save()
    site_settings.company_address = address_usa
    site_settings.include_taxes_in_prices = taxes_in_prices
    site_settings.save()
    line = checkout_with_item.lines.first()
    product = line.variant.product
    product.metadata = {}  # TODO consider adding ATE fields here
    product.save()
    product.product_type.save()
    discounts = [discount_info] if with_discount else None
    line = checkout_with_item.lines.first()
    total = manager.calculate_checkout_line_total(line, discounts,)
    total = quantize_price(total, total.currency)
    assert total == TaxedMoney(
        net=Money(expected_net, "USD"), gross=Money(expected_gross, "USD")
    )


@pytest.mark.vcr
@pytest.mark.parametrize(
    "with_discount, expected_net, expected_gross, taxes_in_prices",
    [(False, "30.00", "32.29", True), ],
)
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_calculate_checkout_line_total_metadata(
    reset_sequences,  # pylint: disable=unused-argument
    with_discount,
    expected_net,
    expected_gross,
    taxes_in_prices,
    discount_info,
    checkout_with_item,
    address_usa_tx,
    address_usa,
    site_settings,
    shipping_zone,
    plugin_configuration,
):
    plugin_configuration()
    manager = get_plugins_manager()

    checkout_with_item.shipping_address = address_usa_tx
    checkout_with_item.shipping_method = shipping_zone.shipping_methods.get()
    checkout_with_item.save()
    site_settings.company_address = address_usa
    site_settings.include_taxes_in_prices = taxes_in_prices
    site_settings.save()
    line = checkout_with_item.lines.first()
    product = line.variant.product
    product.metadata = {}
    product.save()
    product.product_type.save()
    discounts = [discount_info] if with_discount else None
    line = checkout_with_item.lines.first()

    manager.calculate_checkout_line_total(line, discounts,)

    checkout = Checkout.objects.filter(token=line.checkout.token).first()
    assert checkout.metadata[get_metadata_key("itemized_taxes")] == '[{"TransactionTaxAmounts": [], "SequenceId": 1, "TransactionLine": 1, "InvoiceLine": 1, "CountryCode": "USA", "Jurisdiction": "TX", "LocalJurisdiction": "48", "ProductCategory": 0.0, "TaxingLevel": "STA", "TaxType": "S", "RateType": "G", "RateSubtype": "NONE", "CalculationTypeInd": "P", "TaxRate": 0.0625, "TaxQuantity": 0.0, "TaxAmount": 1.73, "TaxExemptionInd": "N", "SalesTaxBaseAmount": 27.71, "LicenseNumber": "", "RateDescription": "TX STATE TAX - TEXAS", "Currency": "USD", "SubtotalInd": "C", "StatusCode": "ACTIVE", "QuantityInd": "B"}, {"TransactionTaxAmounts": [], "SequenceId": 2, "TransactionLine": 1, "InvoiceLine": 1, "CountryCode": "USA", "Jurisdiction": "TX", "LocalJurisdiction": "05000", "ProductCategory": 0.0, "TaxingLevel": "CIT", "TaxType": "S", "RateType": "G", "RateSubtype": "NONE", "CalculationTypeInd": "P", "TaxRate": 0.01, "TaxQuantity": 0.0, "TaxAmount": 0.28, "TaxExemptionInd": "N", "SalesTaxBaseAmount": 27.71, "LicenseNumber": "", "RateDescription": "TX CITY TAX - AUSTIN", "Currency": "USD", "SubtotalInd": "C", "StatusCode": "ACTIVE", "QuantityInd": "B"}, {"TransactionTaxAmounts": [], "SequenceId": 3, "TransactionLine": 1, "InvoiceLine": 1, "CountryCode": "USA", "Jurisdiction": "TX", "LocalJurisdiction": "6000814", "ProductCategory": 0.0, "TaxingLevel": "STJ", "TaxType": "S", "RateType": "G", "RateSubtype": "NONE", "CalculationTypeInd": "P", "TaxRate": 0.01, "TaxQuantity": 0.0, "TaxAmount": 0.28, "TaxExemptionInd": "N", "SalesTaxBaseAmount": 27.71, "LicenseNumber": "", "RateDescription": "TX SPECIAL TAX - AUSTIN MTA TRANSIT", "Currency": "USD", "SubtotalInd": "C", "StatusCode": "ACTIVE", "QuantityInd": "B"}]'


@pytest.mark.vcr
@pytest.mark.parametrize(
    "with_discount, expected_net, expected_gross, voucher_amount, taxes_in_prices",
    [(False, "43.98", "48.95", "0.0", False), ],
)
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_calculate_checkout_total(
    reset_sequences,  # pylint: disable=unused-argument
    with_discount,
    expected_net,
    expected_gross,
    voucher_amount,
    taxes_in_prices,
    checkout_with_item,
    product_with_single_variant,
    discount_info,
    shipping_zone,
    address_usa_tx,
    address_usa,
    site_settings,
    monkeypatch,
    plugin_configuration,
    non_default_category,
):
    plugin_configuration()
    # Required ATE variant data
    metadata = {
        get_metadata_key("UnitQuantity"): 1,
    }
    ProductVariant.objects.filter(sku="SKU_SINGLE_VARIANT").update(
        sku="202127000", private_metadata=metadata
    )
    monkeypatch.setattr(
        "saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin._skip_plugin",
        lambda *_: False,
    )
    manager = get_plugins_manager()
    checkout_with_item.shipping_address = address_usa_tx
    checkout_with_item.save()
    site_settings.company_address = address_usa
    site_settings.include_taxes_in_prices = taxes_in_prices
    site_settings.save()

    voucher_amount = Money(voucher_amount, "USD")
    checkout_with_item.shipping_method = shipping_zone.shipping_methods.get()
    checkout_with_item.discount = voucher_amount
    checkout_with_item.save()

    product_with_single_variant.charge_taxes = False
    product_with_single_variant.category = non_default_category
    product_with_single_variant.save()
    add_variant_to_checkout(
        checkout_with_item, product_with_single_variant.variants.get()
    )

    discounts = [discount_info] if with_discount else None
    lines = fetch_checkout_lines(checkout_with_item)
    total = manager.calculate_checkout_total(
        checkout_with_item, list(checkout_with_item), discounts
    )
    total = quantize_price(total, total.currency)
    assert total == TaxedMoney(
        net=Money(expected_net, "USD"), gross=Money(expected_gross, "USD")
    )


@patch("saleor.plugins.avatax.plugin.AvataxPlugin._skip_plugin")
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_calculate_checkout_total_skip(
    skip_mock, checkout_with_item, address_usa, plugin_configuration
):
    skip_mock.return_value = True
    plugin_configuration()
    manager = get_plugins_manager()
    manager.calculate_checkout_total(checkout_with_item, [], [])
    skip_mock.assert_called_once


@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_calculate_checkout_total_invalid_checkout(
    checkout_with_item, address_usa, plugin_configuration
):
    plugin_configuration()
    manager = get_plugins_manager()
    total = manager.calculate_checkout_total(checkout_with_item, [], [])
    assert total == TaxedMoney(
        net=Money("0.00", "USD"), gross=Money("0.00", "USD"))


@pytest.mark.vcr
@pytest.mark.parametrize(
    "expected_net, expected_gross, taxes_in_prices, variant_sku, price, destination, "
    "metadata",
    [
        (
            "172.0",
            "199.59",
            False,
            "202000000",
            172,
            {"city": "Richmond", "postal_code": "23226", "country_area": "VA"},
            {
                "UnitQuantity": 25,
                "CustomNumeric1": 81.46,
                "CustomNumeric2": 85.65,
                "CustomNumeric3": 95.72,
            },
        ),
        (
            "170.00",
            "186.79",
            False,
            "202015500",
            170,
            {"city": "Tempe", "postal_code": "85281", "country_area": "AZ"},
            {
                "UnitQuantity": 18,
                "CustomNumeric1": 102.51,
                "CustomNumeric2": 108.00,
                "CustomNumeric3": 115.25,
            },
        ),
    ],
)
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_calculate_checkout_total_excise_data(
    reset_sequences,  # pylint: disable=unused-argument
    expected_net,
    expected_gross,
    taxes_in_prices,
    variant_sku,
    price,
    destination,
    metadata,
    checkout,
    product,
    shipping_zone,
    address_usa,
    site_settings,
    monkeypatch,
    plugin_configuration,
    cigar_product_type,
):
    plugin_configuration()
    monkeypatch.setattr(
        "saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin._skip_plugin",
        lambda *_: False,
    )
    manager = get_plugins_manager()

    address_usa.city = destination["city"]
    address_usa.postal_code = destination["postal_code"]
    address_usa.country_area = destination["country_area"]
    address_usa.save()

    checkout.shipping_address = address_usa
    checkout.billing_address = address_usa
    shipping_method = shipping_zone.shipping_methods.get()
    shipping_method.price_amount = 0
    shipping_method.save()
    checkout.shipping_method = shipping_method

    metadata = {
        get_metadata_key("UnitQuantity"): metadata["UnitQuantity"],
        get_metadata_key("CustomNumeric1"): metadata["CustomNumeric1"],
        get_metadata_key("CustomNumeric2"): metadata["CustomNumeric2"],
        get_metadata_key("CustomNumeric3"): metadata["CustomNumeric3"],
    }

    product.product_type = cigar_product_type
    product.save()

    variant = product.variants.get()
    variant.sku = variant_sku
    variant.private_metadata = metadata
    variant.price_amount = Decimal(price)
    variant.save()
    add_variant_to_checkout(checkout, variant, 1)
    checkout.save()

    site_settings.company_address = address_usa
    site_settings.include_taxes_in_prices = taxes_in_prices
    site_settings.save()

    lines = fetch_checkout_lines(checkout)
    total = manager.calculate_checkout_total(checkout, list(checkout), [])
    total = quantize_price(total, total.currency)
    assert total == TaxedMoney(
        net=Money(expected_net, "USD"), gross=Money(expected_gross, "USD")
    )


@pytest.mark.vcr
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_preprocess_order_creation(
    checkout_with_item,
    address,
    address_usa_tx,
    site_settings,
    shipping_zone,
    discount_info,
    plugin_configuration,
):
    plugin_configuration()
    manager = get_plugins_manager()
    site_settings.company_address = address
    site_settings.save()

    checkout_with_item.shipping_address = address_usa_tx
    checkout_with_item.shipping_method = shipping_zone.shipping_methods.get()
    checkout_with_item.save()
    discounts = [discount_info]
    manager.preprocess_order_creation(checkout_with_item, discounts)


@pytest.mark.vcr
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_preprocess_order_creation_wrong_data(
    checkout_with_item, address, shipping_zone, plugin_configuration,
):
    plugin_configuration()

    manager = get_plugins_manager()

    checkout_with_item.shipping_address = address
    checkout_with_item.shipping_method = shipping_zone.shipping_methods.get()
    checkout_with_item.save()
    discounts = []
    with pytest.raises(TaxError) as e:
        manager.preprocess_order_creation(checkout_with_item, discounts)
    # Fails due to no ATE scenario these from/to addresses
    assert "No Scenario record found" in e._excinfo[1].args[0]


def test_api_post_request_handles_request_errors(product, monkeypatch):
    mocked_response = Mock(side_effect=RequestException())
    monkeypatch.setattr(
        "saleor.plugins.avatax.excise.utils.requests.post", mocked_response
    )

    config = AvataxConfiguration(
        username_or_account="test", password_or_license="test", use_sandbox=False,
    )
    url = "https://www.avatax.api.com/some-get-path"

    response = api_post_request(url, {}, config)

    assert mocked_response.called
    assert response == {}


def test_api_post_request_handles_json_errors(product, monkeypatch):
    mocked_response = Mock(side_effect=JSONDecodeError("", "", 0))
    monkeypatch.setattr(
        "saleor.plugins.avatax.excise.utils.requests.post", mocked_response
    )

    config = AvataxConfiguration(
        username_or_account="test", password_or_license="test", use_sandbox=False,
    )
    url = "https://www.avatax.api.com/some-get-path"

    response = api_post_request(url, {}, config)

    assert mocked_response.called
    assert response == {}


@pytest.mark.vcr
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
@patch("saleor.plugins.avatax.excise.plugin.api_post_request_task.delay")
def test_order_created_calls_task(
    api_post_request_task_mock,
    order_with_lines,
    address,
    address_usa_tx,
    site_settings,
    shipping_zone,
    plugin_configuration,
):
    config = plugin_configuration()
    manager = get_plugins_manager()

    site_settings.company_address = address
    site_settings.save()

    order_with_lines.shipping_address = address_usa_tx
    order_with_lines.shipping_method = shipping_zone.shipping_methods.get()
    order_with_lines.save()

    manager.order_created(order_with_lines)

    transaction_url = "https://excisesbx.avalara.com/api/v1/AvaTaxExcise/transactions/create"
    data = get_order_request_data(order_with_lines)
    conf = {
        data["name"]: data["value"] for data in config.configuration
    }
    configuration = {
        'username_or_account': conf["Username or account"],
        'password_or_license': conf["Password or license"], 'use_sandbox': True,
        'company_name': conf["Company name"],
        'autocommit': False
    }

    api_post_request_task_mock.assert_called_once_with(
        transaction_url, asdict(data), configuration, order_with_lines.id
    )


@pytest.mark.vcr
@override_settings(PLUGINS=["saleor.plugins.avatax.excise.plugin.AvataxExcisePlugin"])
def test_order_created(
    order_with_lines,
    product,
    shipping_zone,
    address_usa_tx,
    site_settings,
    plugin_configuration,
    cigar_product_type,
):
    plugin_configuration()
    manager = get_plugins_manager()

    site_settings.company_address = address_usa_tx
    site_settings.save()

    order_with_lines.shipping_address = address_usa_tx
    order_with_lines.shipping_method = shipping_zone.shipping_methods.get()
    shipping_method = shipping_zone.shipping_methods.get()
    shipping_method.price_amount = 0
    shipping_method.save()
    order_with_lines.shipping_method = shipping_method

    product.product_type = cigar_product_type
    product.save()

    variant = product.variants.first()
    variant.sku = "202015500"
    variant.price_amount = Decimal(170)
    variant.save()

    for order_line in order_with_lines.lines.all():
        order_line.product_name = product.name
        order_line.variant_name = variant.name
        order_line.product_sku = variant.sku
        order_line.variant = variant
        order_line.save()

    order_with_lines.save()

    manager.order_created(order_with_lines)
