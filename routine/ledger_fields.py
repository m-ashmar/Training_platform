"""`product` on a ledger row: which program a routine-product row paid for (B-169).

Every ledger row of a program's sale, checkout or refund names the product in its
metadata (`product_id`, `routine/products.py`), but only by id, so the wallet could not
say what was bought without a second read. The ledger is append-only, so the title is
not written into it (old rows would stay bare, and a title has two languages): the row
is JOINED to its product when it is read, in the reader's language.

Read in ONE query per page (`prime_products`, called by the ledger's list serializer
before the first row renders), so the ledger keeps its constant query count. Imports
nothing from `wallet` at module level: `wallet/serializers.py` imports this.
"""
from rest_framework import serializers


def product_id_of(tx):
    """The product a ledger row's metadata names, or None."""
    metadata = tx.metadata if isinstance(tx.metadata, dict) else {}
    value = metadata.get("product_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def product_block(product) -> dict:
    """`{id, title}`, the title in the active language (the other one when empty)."""
    from django.utils.translation import get_language

    return {"id": product.pk, "title": product.title_in(get_language() or "ar")}


class TransactionProductFields(serializers.Serializer):
    """`product` on a ledger row (BACKEND_BUGS B-169): `{id, title}` on a row that moved
    a routine product's money — the buyer's `routine_product_purchase`, the trainer's
    `routine_product_sale`, a refund's reversal — with the title in the request's
    language; `null` on every other row. It names the program, never the buyer."""

    PRODUCT_FIELDS = ("product",)

    product = serializers.SerializerMethodField()

    def get_field_names(self, declared_fields, info):
        names = list(super().get_field_names(declared_fields, info))
        return names + [n for n in self.PRODUCT_FIELDS if n not in names]

    def prime_products(self, rows):
        """Read every product `rows` name in one query, before rendering starts."""
        from .product_models import RoutineProduct

        ids = {pk for pk in (product_id_of(tx) for tx in rows) if pk is not None}
        self.context["products_by_id"] = (
            {p.pk: p for p in RoutineProduct.objects.filter(pk__in=ids)} if ids else {})

    def get_product(self, obj):
        pk = product_id_of(obj)
        if pk is None:
            return None
        found = self.context.get("products_by_id")
        if found is None:
            from .product_models import RoutineProduct

            product = RoutineProduct.objects.filter(pk=pk).first()
        else:
            product = found.get(pk)
        return product_block(product) if product is not None else None
