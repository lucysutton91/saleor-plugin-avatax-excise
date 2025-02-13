import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Union
from urllib.parse import urljoin
from uuid import UUID

import opentracing
import opentracing.tags
import requests
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from requests.auth import HTTPBasicAuth

from saleor.checkout import base_calculations
from saleor.checkout.models import Checkout


try:
    from saleor.checkout.utils import fetch_checkout_lines
except ImportError:
    from .compat import fetch_checkout_lines
from saleor.core.taxes import TaxError
from saleor.plugins.avatax import (
    CACHE_KEY,
    CACHE_TIME,
    AvataxConfiguration,
    api_get_request,
    taxes_need_new_fetch,
    _retrieve_from_cache
)

if TYPE_CHECKING:
    # flake8: noqa
    from saleor.account.models import Address
    from saleor.checkout.models import Checkout, CheckoutLine
    from saleor.order.models import Order
    from saleor.product.models import (
        Product,
        ProductType,
        ProductVariant,
        ProductVariantChannelListing,
    )

logger = logging.getLogger(__name__)


TRANSACTION_TYPE = "DIRECT"  # Must be DIRECT for direct to consumer e-commerece


def get_metadata_key(key_name: str):
    """Namespace metadata key names: PLUGIN_ID:Key."""
    return "mirumee.taxes.avalara_excise:" + key_name


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def get_api_url(use_sandbox=True) -> str:
    """Based on settings return sanbox or production url."""
    if use_sandbox:
        return "https://excisesbx.avalara.com/api/v1/"
    return "https://excise.avalara.net/api/v1/"


def api_post_request(
    url: str, data: Optional[Dict[str, Any]], config: AvataxConfiguration
) -> Dict[str, Any]:
    response = None
    try:
        auth = HTTPBasicAuth(config.username_or_account,
                             config.password_or_license)
        headers = {
            "x-company-id": config.company_name,
            "Content-Type": "application/json",
        }
        formatted_data = json.dumps(data, cls=EnhancedJSONEncoder)
        response = requests.post(url, headers=headers,
                                 auth=auth, data=formatted_data,)
        logger.debug("Hit to Avatax Excise to calculate taxes %s", url)
        json_response = response.json()
        if json_response.get("Status") == "Errors found":
            logger.exception(
                "Avatax Excise response contains errors %s", json_response)
            return json_response

    except requests.exceptions.RequestException:
        logger.exception("Fetching taxes failed %s", url)
        return {}
    except json.JSONDecodeError:
        content = response.content if response else "Unable to find the response"
        logger.exception(
            "Unable to decode the response from Avatax Excise. Response: %s", content
        )
        return {}
    return json_response  # type: ignore


@dataclass
class TransactionLine:
    InvoiceLine: int
    ProductCode: str
    UnitPrice: Decimal
    UnitOfMeasure: str
    BilledUnits: Decimal
    AlternateUnitPrice: Optional[Decimal]
    TaxIncluded: bool
    UnitQuantity: Optional[int]
    UnitQuantityUnitOfMeasure: Optional[str]
    DestinationCountryCode: str
    """ISO 3166-1 alpha-3 code"""
    DestinationJurisdiction: str
    DestinationAddress1: Optional[str]
    DestinationAddress2: Optional[str]
    DestinationCounty: Optional[str]
    DestinationCity: str
    DestinationPostalCode: str
    SaleCountryCode: str
    SaleAddress1: Optional[str]
    SaleAddress2: Optional[str]
    SaleJurisdiction: str
    SaleCounty: Optional[str]
    SaleCity: str
    SalePostalCode: str

    OriginCountryCode: str
    OriginJurisdiction: str
    OriginCounty: str
    OriginCity: str
    OriginPostalCode: str
    OriginAddress1: str
    OriginAddress2: Optional[str]

    CustomString1: Optional[str]
    CustomString2: Optional[str]
    CustomString3: Optional[str]
    CustomNumeric1: Optional[Decimal]
    CustomNumeric2: Optional[Decimal]
    CustomNumeric3: Optional[Decimal]


@dataclass
class TransactionCreateRequestData:
    EffectiveDate: str
    InvoiceDate: str
    TitleTransferCode: str
    TransactionType: str
    TransactionLines: List[TransactionLine]
    InvoiceNumber: Optional[str] = None


def generate_request_data(transaction_type: str, lines: List[TransactionLine], invoice_number: Optional[str]):
    today_date = str(date.today())  # Does not seem timezone safe
    data = TransactionCreateRequestData(
        EffectiveDate=today_date,
        InvoiceDate=today_date,
        InvoiceNumber=invoice_number,
        TitleTransferCode="DEST",
        TransactionType=transaction_type,
        TransactionLines=lines,
    )

    return data


def append_line_to_data(
    data: List[TransactionLine],
    line_id: int,
    quantity: int,
    tax_included: bool,
    variant: "ProductVariant",
    shipping_address: "Address",
):
    """Abstract line data regardless of Checkout or Order."""
    stock = variant.stocks.for_country(shipping_address.country).first()
    warehouse = stock.warehouse
    cost_price = variant.cost_price.amount if variant.cost_price else None
    data.append(
        TransactionLine(
            InvoiceLine=line_id,
            ProductCode=variant.sku,
            UnitPrice=variant.price.amount,
            UnitOfMeasure=variant.product.product_type.get_value_from_private_metadata(
                get_metadata_key("UnitOfMeasure")
            ),
            BilledUnits=Decimal(quantity),
            AlternateUnitPrice=cost_price,
            TaxIncluded=tax_included,
            UnitQuantity=variant.get_value_from_private_metadata(
                get_metadata_key("UnitQuantity")
            ),
            UnitQuantityUnitOfMeasure=variant.product.product_type.get_value_from_private_metadata(
                get_metadata_key("UnitQuantityUnitOfMeasure")
            ),
            DestinationCountryCode=shipping_address.country.alpha3,
            DestinationJurisdiction=shipping_address.country_area,
            DestinationAddress1=shipping_address.street_address_1,
            DestinationAddress2=shipping_address.street_address_2,
            DestinationCity=shipping_address.city,
            DestinationCounty=shipping_address.city_area,
            DestinationPostalCode=shipping_address.postal_code,
            SaleCountryCode=shipping_address.country.alpha3,
            SaleJurisdiction=shipping_address.country_area,
            SaleAddress1=shipping_address.street_address_1,
            SaleAddress2=shipping_address.street_address_2,
            SaleCity=shipping_address.city,
            SaleCounty=shipping_address.city_area,
            SalePostalCode=shipping_address.postal_code,
            OriginCountryCode=warehouse.address.country.alpha3,
            OriginJurisdiction=warehouse.address.country_area,
            OriginAddress1=warehouse.address.street_address_1,
            OriginAddress2=warehouse.address.street_address_2,
            OriginCity=warehouse.address.city,
            OriginCounty=warehouse.address.city_area,
            OriginPostalCode=warehouse.address.postal_code,
            CustomString1=variant.get_value_from_private_metadata(
                get_metadata_key("CustomString1")
            ),
            CustomString2=variant.get_value_from_private_metadata(
                get_metadata_key("CustomString2")
            ),
            CustomString3=variant.get_value_from_private_metadata(
                get_metadata_key("CustomString3")
            ),
            CustomNumeric1=variant.get_value_from_private_metadata(
                get_metadata_key("CustomNumeric1")
            ),
            CustomNumeric2=variant.get_value_from_private_metadata(
                get_metadata_key("CustomNumeric2")
            ),
            CustomNumeric3=variant.get_value_from_private_metadata(
                get_metadata_key("CustomNumeric3")
            ),
        )
    )


def get_checkout_lines_data(
    checkout: "Checkout", discounts=None
) -> List[TransactionLine]:
    data: List[TransactionLine] = []
    lines_info = fetch_checkout_lines(checkout)
    tax_included = Site.objects.get_current().settings.include_taxes_in_prices
    shipping_address = checkout.shipping_address
    if shipping_address is None:
        raise TaxError("Shipping address required for ATE tax calculation")

    for line_info in lines_info:
        append_line_to_data(
            data,
            line_info.line.id,
            line_info.line.quantity,
            tax_included,
            line_info.line.variant,
            shipping_address,
        )
    return data


def get_order_lines_data(order: "Order", discounts=None) -> List[TransactionLine]:

    data: List[TransactionLine] = []
    order_lines = order.lines.all()

    tax_included = Site.objects.get_current().settings.include_taxes_in_prices
    shipping_address = order.shipping_address
    if shipping_address is None:
        raise TaxError("Shipping address required for ATE tax calculation")

    for line in order_lines:
        variant = line.variant
        if variant is None:
            continue

        append_line_to_data(
            data,
            line.id,
            line.quantity,
            tax_included,
            variant,
            shipping_address,
        )
    return data


def generate_request_data_from_checkout(
    checkout: "Checkout", transaction_type=TRANSACTION_TYPE, discounts=None,
):
    lines = get_checkout_lines_data(checkout, discounts)
    data = generate_request_data(
        transaction_type, lines=lines, invoice_number=None)
    return data


def generate_request_data_from_order(
    order: "Order", transaction_type=TRANSACTION_TYPE, discounts=None,
):
    lines = get_order_lines_data(order, discounts)
    data = generate_request_data(
        transaction_type, lines=lines, invoice_number=order.pk,)
    return data


def _fetch_new_taxes_data(
    data: Dict[str, Dict], data_cache_key: str, config: AvataxConfiguration
):
    transaction_url = urljoin(
        get_api_url(config.use_sandbox), "AvaTaxExcise/transactions/create"
    )
    with opentracing.global_tracer().start_active_span(
        "avatax_excise.transactions.create"
    ) as scope:
        span = scope.span
        span.set_tag(opentracing.tags.COMPONENT, "tax")
        span.set_tag("service.name", "avatax_excise")
        response = api_post_request(transaction_url, data, config)
    if response and response.get("Status") == "Success":
        cache.set(data_cache_key, (data, response), CACHE_TIME)
    else:
        # cache failed response to limit hits to avatax.
        cache.set(data_cache_key, (data, response), 10)
    return response


def get_cached_response_or_fetch(
    data: Dict[str, Dict],
    token_in_cache: str,
    config: AvataxConfiguration,
    force_refresh: bool = False,
):
    """Try to find response in cache.

    Return cached response if requests data are the same. Fetch new data in other cases.
    """
    data_cache_key = CACHE_KEY + token_in_cache
    if taxes_need_new_fetch(data, token_in_cache) or force_refresh:
        response = _fetch_new_taxes_data(data, data_cache_key, config)
    else:
        _, response = cache.get(data_cache_key)

    return response


def get_checkout_tax_data(
    checkout: "Checkout", discounts, config: AvataxConfiguration
) -> Dict[str, Any]:
    data = generate_request_data_from_checkout(checkout, discounts=discounts)
    return get_cached_response_or_fetch(data, str(checkout.token), config)


def get_order_request_data(order: "Order", transaction_type=TRANSACTION_TYPE):
    lines = get_order_lines_data(order)
    data = generate_request_data(
        transaction_type=transaction_type, lines=lines, invoice_number=order.pk,)
    return data


def get_order_tax_data(
    order: "Order", config: AvataxConfiguration, force_refresh=False
) -> Dict[str, Any]:
    data = generate_request_data_from_order(order)

    response = get_cached_response_or_fetch(
        data, "order_%s" % order.token, config, force_refresh
    )
    if response.get("Status") != "Success":
        transaction_errors = response.get("TransactionErrors")
        customer_msg = ""
        if isinstance(transaction_errors, list):
            for error in transaction_errors:
                error_message = error.get("ErrorMessage")
                if error_message:
                    customer_msg += error_message
                error_code = response.get("ErrorCode", "")
                logger.warning(
                    "Unable to calculate taxes for order %s, error_code: %s, "
                    "error_msg: %s",
                    order.token,
                    error_code,
                    error_message,
                )
        raise TaxError(customer_msg)
    return response


def _retrieve_meta_data_from_cache(token):
    cached_data = cache.get(token)
    return cached_data


def metadata_requires_update(
    metadata: str,
    token_in_cache: str,
    force_refresh: bool = False,
):
    """Check if Checkout metadata needs to be reset.

    The itemized taxes from ATE are stored in a cache. If an object doesn't exist in cache
    or something has changed, taxes need to be refetched.
    """
    if force_refresh:
        return True

    cached_metadata = _retrieve_meta_data_from_cache(token_in_cache)

    if not cached_metadata:
        return True

    if cached_metadata != metadata:
        return True

    return False


def process_checkout_metadata(
    metadata: str,
    checkout: "Checkout",
    force_refresh: bool = False,
    cache_time: int = CACHE_TIME
):
    """Check for Checkout metadata in cache.

    Do nothing if metadata are the same. Set new metadata in other cases.
    """
    checkout_token = checkout.token
    data_cache_key = "checkout_metadata_" + str(checkout_token)
    tax_item = {get_metadata_key("itemized_taxes"): metadata}

    if metadata_requires_update(tax_item, data_cache_key) or force_refresh:

        checkout_obj = Checkout.objects.filter(token=checkout_token).first()
        if checkout_obj:
            checkout_obj.store_value_in_metadata(items=tax_item)
            checkout_obj.save()
            cache.set(data_cache_key, tax_item, cache_time)
