"""Static synthetic grocery catalog for the laya-research datasheet.

Everything in this module is deterministic.  Module constants never change
between runs, and the dimension builders (:func:`build_stores`,
:func:`build_products`) depend only on the ``random.Random`` instance they are
handed, so the same ``LAYA_SEED`` always yields the same stores, the same
products (ids 1..N) and the same ``sku`` values.

Contents
--------
``CATEGORIES``      12 grocery categories, each with >= 4 subcategories, a
                    plausible price band, a category cost ratio band
                    (0.62-0.88), an elasticity band around -1.6 and a weekend
                    lift band (1.05-1.25).
``PRODUCT_NAMES``   realistic item-name pool keyed by ``(category, subcategory)``
                    - the words full-text search has to bite on.
``SUBCATEGORY_UOM`` which unit of measure each subcategory is sold in, and
                    ``PACK_SIZES`` the pack sizes available per unit.
``QUALITY_MODIFIERS``  quality words ("Organic", "Free Range", "Wholemeal",
                    "Extra Virgin", ...) injected in front of some names.
``NATIONAL_BRANDS`` / ``HOUSE_BRANDS``  >= 250 distinct brands: invented
                    national-style brands plus retailer private-label lines.
``REGIONS``         region -> city map used by the store builder.
``FORMAT_SPECS``    store formats with demand factors and size bands.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

# --- frozen knobs -----------------------------------------------------------

MIN_PRICE = 0.49
MAX_PRICE = 24.99
PRIVATE_LABEL_SHARE = 0.35
SKU_FORMAT = "SKU-%06d"

#: Anchor for store opening dates - a constant, never the wall clock, so the
#: store dimension is byte-identical across runs.
STORE_EPOCH = date(2025, 12, 31)


def _names(block: str) -> tuple[str, ...]:
    """Split a comma separated block into a tuple of stripped names."""
    return tuple(part.strip() for part in block.replace("\n", " ").split(",") if part.strip())


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Category:
    """A grocery category and the model bands the generator samples from."""

    name: str
    weight: float
    subcategories: tuple[str, ...]
    price_range: tuple[float, float]
    cost_ratio: tuple[float, float]
    elasticity: tuple[float, float]
    weekend_lift: tuple[float, float]
    perishable_p: float
    units_per_store_day: tuple[float, float]
    brands: tuple[str, ...]


def _cat(name, weight, subs, price, cost, elast, weekend, perish, velocity, brands):
    return Category(
        name=name,
        weight=weight,
        subcategories=_names(subs),
        price_range=price,
        cost_ratio=cost,
        elasticity=elast,
        weekend_lift=weekend,
        perishable_p=perish,
        units_per_store_day=velocity,
        brands=_names(brands),
    )


CATEGORIES: tuple[Category, ...] = (
    _cat(
        "Produce", 0.08,
        """Fresh Fruit, Fresh Vegetables, Salads & Herbs, Organic Produce,
           Prepared Fruit, Potatoes & Roots""",
        (0.49, 5.99), (0.62, 0.72), (-1.9, -1.3), (1.18, 1.25), 0.95, (1.5, 7.0),
        """Sunfield Farms, Greenacre Growers, Orchard Lane, Vale Harvest, Root & Vine,
           Meadowfresh, Kestrel Produce, Harvest Moon, Terra Verde, Bloom & Bough,
           Copper Field, Willowbrook, Sunrise Growers, Clayfield, Fernhill Farms,
           Amber Grove, Riverbank Produce, Northwind Organics""",
    ),
    _cat(
        "Dairy & Eggs", 0.07,
        "Milk, Cheese, Yogurt & Desserts, Butter & Spreads, Eggs, Cream, Plant Alternatives",
        (0.69, 8.99), (0.70, 0.80), (-1.6, -1.1), (1.08, 1.14), 0.80, (1.2, 6.0),
        """Dalefarm, Meadowgate, Cloverdale Creamery, Ashcombe Dairy, Golden Yolk,
           Butterbrook, Hillside Herd, Elmwood Dairy, Creamery Nine, Pasture Gold,
           Brookfield Farm, Snowdrop Dairy, Wren Valley, Thistlebank, Oakhaven,
           Dairy Lane, Copper Kettle Creamery, Willowherd""",
    ),
    _cat(
        "Bakery", 0.05,
        "Bread, Rolls & Buns, Cakes & Pastries, Morning Goods, Flatbreads & Wraps, Gluten Free Bakery",
        (0.55, 5.49), (0.62, 0.74), (-1.8, -1.2), (1.15, 1.22), 0.85, (0.9, 4.0),
        """Millstone Bakeries, Hearth & Crust, Bloomfield Bakehouse, The Daily Loaf,
           Cornfield Craft, Rye & Co, Sunrise Bakehouse, Old Mill Bakers, Flour & Water,
           Baker Street Co, Golden Ear, Wild Yeast Bakery, Crust & Crumb, Stone Oven,
           Bramley Bakers, Wheatfield, Harvest Loaf, Poppyseed Pantry""",
    ),
    _cat(
        "Meat & Seafood", 0.06,
        "Beef & Lamb, Pork, Poultry, Sausages & Bacon, Fish, Shellfish, Plant Based Meat",
        (1.99, 15.99), (0.68, 0.80), (-1.9, -1.2), (1.18, 1.25), 0.90, (0.6, 3.5),
        """Huntsman Butchery, Field & Blade, Red Barn Meats, Coastline Catch,
           Deep Blue Fisheries, Pasture Prime, Butchers Block, Highland Reared,
           Riverbank Smokehouse, Silver Fin, The Chop House, Northshore Seafood,
           Green Pastures, Ironbark Grills, Tidewater, Falconer Farms,
           Cobblestone Butchers, Sea Harvest Co""",
    ),
    _cat(
        "Frozen", 0.05,
        """Frozen Vegetables, Frozen Fruit, Frozen Pizza, Frozen Ready Meals, Ice Cream,
           Frozen Chips & Potatoes, Frozen Fish""",
        (0.99, 8.99), (0.66, 0.78), (-2.0, -1.4), (1.05, 1.12), 0.45, (0.5, 3.0),
        """Arctic Larder, Frostline Foods, Glacier Kitchen, Polar Pantry, Snowcap,
           Icefield Foods, Deep Freeze Co, Northstar Frozen, Crystal Bay,
           Freezer Aisle Co, Tundra Treats, Chill & Serve, Blizzard Bites,
           Permafrost Foods, Icelandia, Frozen Meadow, Coldstore Kitchen, Aurora Ice""",
    ),
    _cat(
        "Pantry", 0.19,
        """Pasta & Noodles, Rice & Grains, Tinned Food, Oils & Vinegars, Sauces & Condiments,
           Herbs & Spices, Breakfast Cereals""",
        (0.49, 9.99), (0.72, 0.84), (-1.7, -1.2), (1.06, 1.12), 0.04, (0.8, 5.0),
        """Pantry Pride, Golden Grain, Casa Verde, Spice Route, Olive & Thyme, Sunharvest,
           Copper Pot, The Larder Co, Amber Jar, Silk Road Foods, Terracotta Kitchen,
           Wild Meadow Preserves, Grainhouse, Salt & Cedar, Basilico, Nomad Pantry,
           Harvest Table, Clay Oven Foods""",
    ),
    _cat(
        "Beverages", 0.13,
        "Water, Soft Drinks, Juice, Coffee, Tea, Beer & Cider, Wine",
        (0.49, 19.99), (0.70, 0.86), (-2.0, -1.3), (1.14, 1.22), 0.12, (0.7, 4.5),
        """Clearspring Waters, Aqua Vale, Bolt Cola, Fizzwell, Orchard Press,
           Beanhouse Roasters, Leaf & Kettle, Highland Roast, Hopfield Brewing,
           Cider Barn, Vineyard Nine, Sunlit Cellars, Cascade Springs, Brew & Bean,
           Morning Ritual, Copper Still, Meridian Wines, Juniper & Tonic""",
    ),
    _cat(
        "Snacks", 0.13,
        "Crisps & Chips, Chocolate, Biscuits, Nuts & Seeds, Sweets & Candy, Crackers & Rice Cakes, Popcorn",
        (0.49, 4.99), (0.68, 0.82), (-2.2, -1.6), (1.15, 1.25), 0.04, (0.6, 4.0),
        """Crunchwell, Salt & Vinegar Co, Cocoa Ridge, Golden Bar, Nutfarm,
           The Biscuit Tin, Sweet Harbour, Popcorn Lane, Savoury Snacks Co, Hazel & Co,
           Crispy Corner, Choco Loom, Trailhead, Sugarplum, Oat & Honey,
           Cracker Jacks Ltd, Snacksmith, Buttercrunch""",
    ),
    _cat(
        "Household", 0.08,
        """Laundry, Cleaning Products, Kitchen Roll & Tissues, Dishwashing, Air Fresheners,
           Bin Liners""",
        (0.89, 12.99), (0.74, 0.88), (-1.5, -0.9), (1.05, 1.10), 0.0, (0.3, 2.0),
        """Brightwash, Sparkle Co, Homeguard, Pure Clean, Freshline, Tidyhome, EcoRinse,
           Laundry Lane, Shield & Shine, Clean Slate, Bubble Works, Rinsewell,
           Home Comfort, Kitchen Bright, Suds & Co, Dustaway, Polish Point, Washday""",
    ),
    _cat(
        "Personal Care", 0.08,
        "Hair Care, Skin Care, Oral Care, Bath & Shower, Deodorants, Shaving, Menstrual Care",
        (0.99, 14.99), (0.70, 0.86), (-1.6, -1.0), (1.05, 1.10), 0.0, (0.2, 1.5),
        """Silkleaf, Pure Ritual, Botanica Nine, Derma Calm, Fresh Bloom, Velvet & Sage,
           Hairloom, Glow Lab, Mint & Myrrh, Skinfolk, Aura Care, Cedar & Rose,
           The Bath House, Shinewell, Nurture Co, Clearform, Wellbeing Works, Soft Tide""",
    ),
    _cat(
        "Baby", 0.05,
        "Baby Food, Baby Formula, Nappies, Baby Wipes, Baby Bath & Care, Baby Snacks",
        (1.49, 24.99), (0.72, 0.86), (-1.2, -0.7), (1.05, 1.10), 0.06, (0.15, 1.2),
        """Tiny Steps, Little Sprout, Nurture Baby, Cuddle Co, Babybloom, First Bites,
           Soft Nest, Tiny Tums, Pram & Co, Little Fern, Gentle Care Baby, Baby Meadow,
           Snugglebug, Milestone Baby, Hushabye, Tiny Explorers, Little Harbour,
           Newleaf Baby""",
    ),
    _cat(
        "Deli", 0.03,
        """Cooked Meats, Olives & Antipasti, Cheese Counter, Fresh Pasta & Sauces,
           Dips & Spreads, Sushi & Ready to Eat""",
        (1.29, 12.99), (0.66, 0.78), (-2.0, -1.3), (1.15, 1.22), 0.95, (0.25, 1.6),
        """Counter & Co, Deli Nine, Olive Bar, Charcuterie House, The Cheese Room,
           Fresh Slice, Antipasto Co, Riverside Deli, Salt Cured, Mezze Lane,
           Cured & Crafted, The Pickle Jar, Sourdough Deli, Vine Leaf,
           Butchers Counter, Pantry Deli, Tapenade & Co, Cold Cuts Kitchen""",
    ),
)

CATEGORY_BY_NAME: dict[str, Category] = {c.name: c for c in CATEGORIES}
CATEGORY_INDEX: dict[str, int] = {c.name: i for i, c in enumerate(CATEGORIES)}

#: uom -> sampling weight for subcategories with no strong convention.
UOM_WEIGHTS: dict[str, float] = {"each": 1.0, "pack": 0.8, "kg": 0.5, "litre": 0.5}

#: Which unit of measure each subcategory is actually sold in.  Sampling uom
#: independently of the item name would put "Cinnamon Bagels" in litres, so
#: the unit is a property of the subcategory and only genuinely mixed
#: subcategories carry a weighted choice.
SUBCATEGORY_UOM: dict[str, dict[str, float]] = {
    # Produce
    "Fresh Fruit": {"each": 1.0, "pack": 0.5},
    "Fresh Vegetables": {"each": 1.0, "kg": 0.7, "pack": 0.4},
    "Salads & Herbs": {"each": 1.0},
    "Organic Produce": {"each": 1.0, "kg": 0.6, "pack": 0.3},
    "Prepared Fruit": {"each": 1.0, "pack": 0.6},
    "Potatoes & Roots": {"kg": 1.0, "pack": 0.7, "each": 0.3},
    # Dairy & Eggs
    "Milk": {"litre": 1.0},
    "Cheese": {"each": 1.0, "kg": 0.5, "pack": 0.4},
    "Yogurt & Desserts": {"each": 1.0, "pack": 0.7},
    "Butter & Spreads": {"each": 1.0, "kg": 0.3},
    "Eggs": {"each": 1.0, "pack": 0.8},
    "Cream": {"litre": 1.0, "each": 0.4},
    "Plant Alternatives": {"litre": 1.0, "pack": 0.4},
    # Bakery
    "Bread": {"each": 1.0},
    "Rolls & Buns": {"each": 1.0, "pack": 0.8},
    "Cakes & Pastries": {"each": 1.0, "pack": 0.4},
    "Morning Goods": {"each": 1.0, "pack": 0.7},
    "Flatbreads & Wraps": {"each": 1.0, "pack": 0.6},
    "Gluten Free Bakery": {"each": 1.0},
    # Meat & Seafood
    "Beef & Lamb": {"kg": 1.0, "each": 0.5, "pack": 0.3},
    "Pork": {"kg": 1.0, "each": 0.5},
    "Poultry": {"kg": 1.0, "each": 0.6},
    "Sausages & Bacon": {"kg": 1.0, "each": 0.7, "pack": 0.5},
    "Fish": {"kg": 1.0, "each": 0.7},
    "Shellfish": {"kg": 1.0, "each": 0.6},
    "Plant Based Meat": {"each": 1.0, "pack": 0.5, "kg": 0.3},
    # Frozen
    "Frozen Vegetables": {"kg": 1.0, "each": 0.6},
    "Frozen Fruit": {"kg": 1.0, "each": 0.6},
    "Frozen Pizza": {"each": 1.0},
    "Frozen Ready Meals": {"each": 1.0},
    "Ice Cream": {"each": 1.0, "litre": 0.5},
    "Frozen Chips & Potatoes": {"kg": 1.0, "each": 0.7},
    "Frozen Fish": {"kg": 1.0, "each": 0.7},
    # Pantry
    "Pasta & Noodles": {"pack": 1.0, "kg": 0.4},
    "Rice & Grains": {"kg": 1.0, "pack": 0.6},
    "Tinned Food": {"each": 1.0, "pack": 0.8},
    "Oils & Vinegars": {"litre": 1.0, "each": 0.3},
    "Sauces & Condiments": {"each": 1.0, "pack": 0.4},
    "Herbs & Spices": {"each": 1.0, "pack": 0.3},
    "Breakfast Cereals": {"pack": 1.0, "kg": 0.5},
    # Beverages
    "Water": {"litre": 1.0, "pack": 0.7},
    "Soft Drinks": {"litre": 1.0, "pack": 0.8},
    "Juice": {"litre": 1.0, "pack": 0.4},
    "Coffee": {"pack": 1.0, "kg": 0.3, "each": 0.3},
    "Tea": {"pack": 1.0, "each": 0.3},
    "Beer & Cider": {"litre": 1.0, "pack": 0.7},
    "Wine": {"each": 1.0, "pack": 0.4},
    # Snacks
    "Crisps & Chips": {"pack": 1.0, "each": 0.8},
    "Chocolate": {"each": 1.0, "pack": 0.5},
    "Biscuits": {"each": 1.0, "pack": 0.7},
    "Nuts & Seeds": {"pack": 1.0, "kg": 0.4},
    "Sweets & Candy": {"pack": 1.0, "each": 0.6},
    "Crackers & Rice Cakes": {"each": 1.0, "pack": 0.6},
    "Popcorn": {"pack": 1.0, "each": 0.6},
    # Household
    "Laundry": {"litre": 1.0, "pack": 0.8, "kg": 0.3},
    "Cleaning Products": {"each": 1.0, "litre": 0.5, "pack": 0.4},
    "Kitchen Roll & Tissues": {"pack": 1.0, "each": 0.5},
    "Dishwashing": {"each": 1.0, "litre": 0.6, "pack": 0.7},
    "Air Fresheners": {"each": 1.0},
    "Bin Liners": {"pack": 1.0, "each": 0.4},
    # Personal Care
    "Hair Care": {"each": 1.0, "litre": 0.5},
    "Skin Care": {"each": 1.0, "litre": 0.3},
    "Oral Care": {"each": 1.0, "pack": 0.4},
    "Bath & Shower": {"each": 1.0, "litre": 0.5},
    "Deodorants": {"each": 1.0, "pack": 0.4},
    "Shaving": {"each": 1.0, "pack": 0.4},
    "Menstrual Care": {"pack": 1.0, "each": 0.7},
    # Baby
    "Baby Food": {"each": 1.0, "pack": 0.6, "kg": 0.3},
    "Baby Formula": {"each": 1.0, "kg": 0.4},
    "Nappies": {"pack": 1.0},
    "Baby Wipes": {"pack": 1.0},
    "Baby Bath & Care": {"each": 1.0, "litre": 0.3},
    "Baby Snacks": {"each": 1.0, "pack": 0.5},
    # Deli
    "Cooked Meats": {"kg": 1.0, "pack": 0.6},
    "Olives & Antipasti": {"each": 1.0, "kg": 0.4},
    "Cheese Counter": {"kg": 1.0, "each": 0.4},
    "Fresh Pasta & Sauces": {"each": 1.0, "kg": 0.3},
    "Dips & Spreads": {"each": 1.0},
    "Sushi & Ready to Eat": {"each": 1.0},
}

#: Pack sizes, by uom.  `each` is always exactly one unit, matching the
#: contract's `"uom":"each","pack_size":1.0` pairing; multipacks are `pack`.
PACK_SIZES: dict[str, tuple[float, ...]] = {
    "each": (1.0,),
    "pack": (2.0, 3.0, 4.0, 6.0, 8.0, 9.0, 12.0, 15.0),
    "kg": (0.1, 0.2, 0.25, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0),
    "litre": (0.25, 0.33, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
}

#: Quality words injected in front of some product names so search has
#: "organic", "free range", "wholemeal", "extra virgin" ... to match.  The
#: category entry is the default; a subcategory with its own vocabulary (oils
#: take "Extra Virgin", coffee takes "Single Origin", fish is never "Free
#: Range") overrides it via `SUBCATEGORY_MODIFIERS`.
QUALITY_MODIFIERS: dict[str, tuple[str, ...]] = {
    "Produce": ("Organic", "Hand Selected", "Extra Fine", "Peak Season"),
    "Dairy & Eggs": ("Organic", "Free Range", "Grass Fed", "Farmhouse"),
    "Bakery": ("Wholemeal", "Seeded", "Sourdough", "Stone Baked"),
    "Meat & Seafood": ("Free Range", "Grass Fed", "Outdoor Reared", "Dry Aged"),
    "Frozen": ("Family Size", "Oven Ready", "Chunky", "Extra Crispy"),
    "Pantry": ("Organic", "Classic", "Premium", "Wholewheat"),
    "Beverages": ("Fairtrade", "Organic", "Classic", "Premium"),
    "Snacks": ("Sharing", "Hand Cooked", "Extra Crunchy", "Fairtrade"),
    "Household": ("Concentrated", "Eco", "Extra Strength", "Refill"),
    "Personal Care": ("Sensitive", "Nourishing", "Fragrance Free", "Natural"),
    "Baby": ("Organic", "Sensitive", "Gentle", "Fragrance Free"),
    "Deli": ("Hand Carved", "Freshly Made", "Slow Roasted", "Traditionally Made"),
}

#: Subcategory-specific quality words, overriding the category default.
SUBCATEGORY_MODIFIERS: dict[str, tuple[str, ...]] = {
    # Pantry: only oils are "extra virgin", only grains are "wholegrain".
    "Oils & Vinegars": ("Extra Virgin", "Cold Pressed", "Unfiltered", "Organic"),
    "Rice & Grains": ("Wholegrain", "Basmati", "Long Grain", "Organic"),
    "Pasta & Noodles": ("Wholewheat", "Durum Wheat", "Bronze Cut", "Organic"),
    "Breakfast Cereals": ("Wholewheat", "Multigrain", "Honey", "Organic"),
    "Herbs & Spices": ("Ground", "Whole", "Organic", "Fairtrade"),
    "Sauces & Condiments": ("Reduced Sugar", "Classic", "Organic", "Free Range"),
    "Tinned Food": ("No Added Sugar", "In Water", "Organic", "Classic"),
    # Beverages: "Sparkling" belongs to water, "Single Origin" to coffee.
    "Water": ("Sparkling", "Still", "Spring", "Mineral"),
    "Soft Drinks": ("Diet", "Zero Sugar", "Classic", "Sparkling"),
    "Juice": ("Not From Concentrate", "Freshly Squeezed", "Smooth", "Organic"),
    "Coffee": ("Fairtrade", "Single Origin", "Cold Brew", "Rich Roast"),
    "Tea": ("Fairtrade", "Organic", "Loose Leaf", "Decaffeinated"),
    "Beer & Cider": ("Craft", "Alcohol Free", "Golden", "Dry"),
    "Wine": ("Single Estate", "Reserve", "Organic", "Vintage"),
    # Bakery: cake is not "wholemeal".
    "Bread": ("Wholemeal", "Seeded", "Sourdough", "Stone Baked"),
    "Rolls & Buns": ("Seeded", "Wholemeal", "Sourdough", "Brioche"),
    "Cakes & Pastries": ("Hand Finished", "All Butter", "Freshly Baked", "Indulgent"),
    "Morning Goods": ("Buttermilk", "Cinnamon", "Toasting", "Freshly Baked"),
    "Flatbreads & Wraps": ("Wholemeal", "Seeded", "Stone Baked", "Soft"),
    "Gluten Free Bakery": ("Seeded", "White", "Wholemeal", "Multigrain"),
    # Meat & Seafood: fish is caught, not reared free range.
    "Fish": ("Line Caught", "Wild Caught", "Sustainably Sourced", "Smoked"),
    "Shellfish": ("Wild Caught", "Sustainably Sourced", "Ready To Eat", "Fresh"),
    "Frozen Fish": ("Oven Ready", "Sustainably Sourced", "Breaded", "Family Size"),
    # Dairy & Eggs.
    "Plant Alternatives": ("Unsweetened", "Barista", "Organic", "Fortified"),
    "Cheese": ("Mature", "Extra Mature", "Organic", "Farmhouse"),
    # Snacks.
    "Chocolate": ("Fairtrade", "Single Origin", "Dark", "Smooth"),
    "Biscuits": ("Hand Baked", "All Butter", "Oat", "Chocolate"),
    "Nuts & Seeds": ("Dry Roasted", "Salted", "Unsalted", "Roasted"),
    "Popcorn": ("Sweet", "Salted", "Toffee", "Cinema Style"),
    "Crisps & Chips": ("Hand Cooked", "Sharing", "Kettle Cooked", "Ridge Cut"),
    # Baby: "Stage 1" only applies to food and formula.
    "Baby Food": ("Stage 1", "Stage 2", "Organic", "From 6 Months"),
    "Baby Formula": ("Stage 1", "Stage 2", "Organic", "Ready To Feed"),
    # Deli.
    "Olives & Antipasti": ("Extra Virgin", "Marinated", "Pitted", "Kalamata"),
    "Cooked Meats": ("Hand Carved", "Slow Roasted", "Traditionally Made", "Smoked"),
    "Cheese Counter": ("Aged", "Raw Milk", "Artisan", "Traditionally Made"),
    "Fresh Pasta & Sauces": ("Freshly Made", "Slow Roasted", "Bronze Cut", "Classic"),
}


def modifiers(category: str, subcategory: str) -> tuple[str, ...]:
    """Quality words usable in front of a ``(category, subcategory)`` item."""
    return SUBCATEGORY_MODIFIERS.get(subcategory, QUALITY_MODIFIERS[category])


# ---------------------------------------------------------------------------
# Product name pool, per (category, subcategory)
# ---------------------------------------------------------------------------

PRODUCT_NAMES: dict[tuple[str, str], tuple[str, ...]] = {
    ("Produce", "Fresh Fruit"): _names("""
        Organic Bananas, Fairtrade Bananas, Seedless Red Grapes, Royal Gala Apples,
        Conference Pears, Easy Peeler Oranges, Ripe Avocados, Golden Kiwi Fruit,
        Strawberry Punnet, Blueberries, Mango, Pineapple, Lemons, Limes,
        Cantaloupe Melon, White Nectarines
    """),
    ("Produce", "Fresh Vegetables"): _names("""
        Organic Carrots, Baby Spinach, Curly Kale, Tender Broccoli Florets,
        Vine Ripened Tomatoes, Cucumber, Mixed Peppers, Red Onions, Baking Potatoes,
        Sweet Potatoes, Courgettes, Fine Green Beans, Chestnut Mushrooms, Spring Onions,
        Iceberg Lettuce, Sugar Snap Peas, Butternut Squash
    """),
    ("Produce", "Salads & Herbs"): _names("""
        Mixed Leaf Salad, Rocket & Watercress, Fresh Basil Pot, Coriander Bunch,
        Flat Leaf Parsley, Mint Bunch, Rosemary Sprigs, Living Salad Bowl,
        Caesar Salad Kit, Greek Salad Bowl, Coleslaw Mix, Chives, Dill, Oregano
    """),
    ("Produce", "Organic Produce"): _names("""
        Organic Sweet Potatoes, Organic Avocados, Organic Baby Kale,
        Organic Cherry Tomatoes, Organic Lemons, Organic Blueberries, Organic Broccoli,
        Organic Beetroot, Organic Leeks, Organic Celery, Organic Courgettes,
        Organic Red Apples, Organic Spring Greens, Organic Chestnut Mushrooms
    """),
    ("Produce", "Prepared Fruit"): _names("""
        Freshly Prepared Fruit Salad, Mango Chunks, Pineapple Fingers, Melon Medley,
        Berry Mix, Apple Slices, Coconut Pieces, Pomegranate Seeds, Grapes & Berries Mix,
        Fruit Snack Pots, Smoothie Fruit Mix, Dried Mango Slices
    """),
    ("Produce", "Potatoes & Roots"): _names("""
        Maris Piper Potatoes, Baby New Potatoes, Baking Potatoes, Sweet Potato Wedges,
        Organic Parsnips, Swede, Turnip, Celeriac, Beetroot Bunch, Carrot Batons,
        Jersey Royal Potatoes, Charlotte Potatoes, Chantenay Carrots, Roasting Root Medley
    """),
    ("Dairy & Eggs", "Milk"): _names("""
        Whole Milk, Semi Skimmed Milk, Skimmed Milk, Organic Whole Milk, Filtered Milk,
        Lactose Free Milk, Goats Milk, Long Life Milk, Fresh Pasteurised Whole Milk,
        Channel Island Milk, Milk Alternative Blend, Creamy Whole Milk
    """),
    ("Dairy & Eggs", "Cheese"): _names("""
        Mature Cheddar, Extra Mature Cheddar, Red Leicester, Double Gloucester,
        Wensleydale, Feta Cheese, Mozzarella, Halloumi, Brie, Camembert, Grated Cheddar,
        Cheese Slices, Smoked Applewood Cheddar, Soft Cheese
    """),
    ("Dairy & Eggs", "Yogurt & Desserts"): _names("""
        Greek Style Yogurt, Natural Yogurt, Fat Free Natural Yogurt, Strawberry Yogurt,
        Vanilla Yogurt, Kids Yogurt Pouches, Skyr Yogurt, Rice Pudding, Chocolate Mousse,
        Custard Pots, Yogurt Drinks, Coconut Yogurt Alternative
    """),
    ("Dairy & Eggs", "Butter & Spreads"): _names("""
        Salted Butter, Unsalted Butter, Spreadable Butter, Light Spread,
        Olive Oil Spread, Clotted Cream, Soured Cream, Creme Fraiche, Margarine,
        Garlic Butter, Dairy Free Spread, Baking Block
    """),
    ("Dairy & Eggs", "Eggs"): _names("""
        Free Range Large Eggs, Free Range Medium Eggs, Organic Eggs,
        Heritage Brown Eggs, Duck Eggs, Quail Eggs, Barn Eggs, Mixed Weight Eggs,
        Free Range Egg Whites, Hard Boiled Eggs, Large Eggs, Speckled Eggs
    """),
    ("Dairy & Eggs", "Cream"): _names("""
        Single Cream, Double Cream, Whipping Cream, Extra Thick Double Cream,
        Half Fat Creme Fraiche, Clotted Cream Pot, Squirty Cream,
        Soya Cream Alternative, Pouring Cream, Long Life Double Cream, Brandy Cream,
        Fresh Custard
    """),
    ("Dairy & Eggs", "Plant Alternatives"): _names("""
        Oat Drink, Almond Drink, Soya Drink, Coconut Drink, Rice Drink, Hazelnut Drink,
        Barista Oat Drink, Unsweetened Almond Drink, Pea Protein Drink, Cashew Drink,
        Oat Milk Barista Edition, Organic Soya Drink
    """),
    ("Bakery", "Bread"): _names("""
        Wholemeal Bread, White Bloomer, Seeded Batch Loaf, Sourdough Loaf, Granary Bread,
        Rye Bread, Milk Roll, Tiger Bread, Half and Half Bread, Thick Sliced White Bread,
        Soft White Medium Bread, Organic Wholemeal Bread
    """),
    ("Bakery", "Rolls & Buns"): _names("""
        White Baps, Wholemeal Rolls, Seeded Burger Buns, Hot Dog Rolls, Ciabatta Rolls,
        Sourdough Rolls, Brioche Buns, Bagels, Crumpets, English Muffins, Dinner Rolls,
        Cheese Topped Rolls
    """),
    ("Bakery", "Cakes & Pastries"): _names("""
        Victoria Sponge Cake, Chocolate Fudge Cake, Carrot Cake, Lemon Drizzle Cake,
        Croissants, Pain au Chocolat, Danish Pastries, Cinnamon Swirls, Fruit Scones,
        Bakewell Tarts, Eccles Cakes, Chelsea Buns
    """),
    ("Bakery", "Morning Goods"): _names("""
        Belgian Waffles, American Style Pancakes, Buttermilk Pancakes, Potato Cakes,
        Toasting Waffles, Breakfast Muffins, Fruit Loaf, Hot Cross Buns, Teacakes,
        Brioche Loaf, Croissant Bake, Cinnamon Bagels
    """),
    ("Bakery", "Flatbreads & Wraps"): _names("""
        Tortilla Wraps, Wholemeal Wraps, Naan Bread, Pitta Bread, Plain Chapattis,
        Garlic & Coriander Naan, Seeded Wraps, Kebab Wraps, Fajita Kit Wraps, Mini Pitta,
        Turkish Flatbread, Sourdough Flatbread
    """),
    ("Bakery", "Gluten Free Bakery"): _names("""
        Gluten Free White Bread, Gluten Free Wholemeal Bread, Gluten Free Rolls,
        Gluten Free Bagels, Gluten Free Croissants, Gluten Free Pitta, Gluten Free Wraps,
        Gluten Free Fruit Loaf, Gluten Free Crumpets, Gluten Free Seeded Loaf,
        Gluten Free Sourdough, Gluten Free Baguette
    """),
    ("Meat & Seafood", "Beef & Lamb"): _names("""
        Beef Mince 5% Fat, Beef Mince 20% Fat, Beef Steak, Sirloin Steak, Ribeye Steak,
        Beef Brisket, Rolled Beef Joint, Lamb Chops, Lamb Mince, Leg of Lamb,
        Diced Beef, Beef Burgers
    """),
    ("Meat & Seafood", "Pork"): _names("""
        Pork Chops, Pork Belly Slices, Pork Loin Joint, Pork Mince, Gammon Steaks,
        Diced Pork, Pork Shoulder, Pork Ribs, Pork Tenderloin, Cumberland Sausage Meat,
        Pork Loin Steaks, Slow Cook Pork Shoulder
    """),
    ("Meat & Seafood", "Poultry"): _names("""
        Chicken Breast Fillets, Chicken Thighs, Whole Chicken, Chicken Drumsticks,
        Turkey Mince, Turkey Breast Steaks, Duck Breast, Chicken Wings,
        Free Range Whole Chicken, Chicken Mini Fillets, Turkey Mince 7% Fat,
        Chicken Stir Fry Strips
    """),
    ("Meat & Seafood", "Sausages & Bacon"): _names("""
        Pork Sausages, Cumberland Sausages, Lincolnshire Sausages, Chipolatas,
        Smoked Back Bacon, Unsmoked Streaky Bacon, Bacon Medallions, Chorizo Sausage,
        Beef Sausages, Chicken Sausages, Maple Cured Bacon, Sausage Meat
    """),
    ("Meat & Seafood", "Fish"): _names("""
        Salmon Fillets, Smoked Salmon, Cod Fillets, Haddock Fillets, Tuna Steaks,
        Sea Bass Fillets, Mackerel Fillets, Rainbow Trout, Breaded Cod Fillets,
        Fish Pie Mix, Kippers, Smoked Haddock
    """),
    ("Meat & Seafood", "Shellfish"): _names("""
        King Prawns, Cooked Prawns, Raw Tiger Prawns, Mussels, Scallops, Crab Meat,
        Squid Rings, Lobster Tails, Langoustines, Seafood Mix, Clams, Oysters
    """),
    ("Meat & Seafood", "Plant Based Meat"): _names("""
        Plant Based Mince, Vegan Burgers, Meat Free Sausages, Plant Based Chicken Pieces,
        Tofu Block, Smoked Tofu, Tempeh, Falafel, Vegan Meatballs, Plant Based Nuggets,
        Seitan Pieces, Veggie Burger Patties
    """),
    ("Frozen", "Frozen Vegetables"): _names("""
        Frozen Peas, Garden Peas, Frozen Sweetcorn, Mixed Vegetables, Frozen Spinach,
        Frozen Broccoli, Frozen Roast Potatoes, Frozen Green Beans, Stir Fry Vegetables,
        Frozen Cauliflower, Frozen Sliced Carrots, Frozen Peppers
    """),
    ("Frozen", "Frozen Fruit"): _names("""
        Frozen Berries, Frozen Strawberries, Frozen Blueberries, Frozen Mango Chunks,
        Frozen Raspberries, Frozen Cherries, Smoothie Mix, Frozen Pineapple,
        Frozen Mixed Fruit, Frozen Blackberries, Frozen Avocado Chunks,
        Frozen Banana Slices
    """),
    ("Frozen", "Frozen Pizza"): _names("""
        Margherita Pizza, Pepperoni Pizza, Ham & Pineapple Pizza, Four Cheese Pizza,
        Stonebaked Pizza, Thin & Crispy Pizza, Deep Pan Pizza, Veggie Supreme Pizza,
        Gluten Free Pizza, Garlic Bread Pizza, Sourdough Pizza, Spicy Chicken Pizza
    """),
    ("Frozen", "Frozen Ready Meals"): _names("""
        Chicken Curry Ready Meal, Beef Lasagne, Macaroni Cheese, Vegetable Lasagne,
        Chicken Tikka Masala, Fish Pie, Cottage Pie, Shepherds Pie, Sweet & Sour Chicken,
        Chilli Con Carne, Vegetable Curry, Beef Stew
    """),
    ("Frozen", "Ice Cream"): _names("""
        Vanilla Ice Cream, Chocolate Ice Cream, Strawberry Ice Cream,
        Salted Caramel Ice Cream, Mint Choc Chip Ice Cream, Ice Cream Cones, Ice Lollies,
        Fruit Sorbet, Frozen Yogurt, Cornish Ice Cream, Chocolate Lollies,
        Mini Ice Cream Bars
    """),
    ("Frozen", "Frozen Chips & Potatoes"): _names("""
        Oven Chips, Chunky Chips, Skin On Fries, Curly Fries, Hash Browns,
        Potato Waffles, Croquettes, Sweet Potato Fries, Frozen Roast Potatoes,
        Dauphinoise Potatoes, Potato Smiles, Potato Wedges
    """),
    ("Frozen", "Frozen Fish"): _names("""
        Frozen Cod Fillets, Breaded Fish Fillets, Fish Fingers, Breaded Scampi,
        Frozen Prawns, Salmon Portions, Fish Cakes, Battered Cod,
        Smoked Haddock Fillets, Frozen Mussels, Tempura Prawns, Frozen Fish Pie Mix
    """),
    ("Pantry", "Pasta & Noodles"): _names("""
        Spaghetti, Penne, Fusilli, Conchiglie, Tagliatelle, Wholewheat Spaghetti,
        Lasagne Sheets, Egg Noodles, Rice Noodles, Ramen Noodles, Macaroni,
        Fresh Tortellini
    """),
    ("Pantry", "Rice & Grains"): _names("""
        Basmati Rice, Long Grain Rice, Wholegrain Rice, Jasmine Rice,
        Arborio Risotto Rice, Brown Basmati Rice, Quinoa, Bulgur Wheat, Cous Cous,
        Pearl Barley, Paella Rice, Wild Rice Mix
    """),
    ("Pantry", "Tinned Food"): _names("""
        Chopped Tomatoes, Tinned Plum Tomatoes, Baked Beans, Chickpeas, Kidney Beans,
        Sweetcorn Tin, Tuna Chunks, Sardines, Coconut Milk, Tomato Soup, Mushy Peas,
        Tinned Peaches
    """),
    ("Pantry", "Oils & Vinegars"): _names("""
        Extra Virgin Olive Oil, Olive Oil, Sunflower Oil, Rapeseed Oil, Vegetable Oil,
        Coconut Oil, Sesame Oil, Balsamic Vinegar, White Wine Vinegar, Malt Vinegar,
        Cider Vinegar, Chilli Oil
    """),
    ("Pantry", "Sauces & Condiments"): _names("""
        Tomato Ketchup, Mayonnaise, Brown Sauce, Salad Cream, Soy Sauce,
        Sweet Chilli Sauce, Pesto, Tomato Pasta Sauce, Barbecue Sauce, Mustard,
        Hot Pepper Sauce, Gravy Granules
    """),
    ("Pantry", "Herbs & Spices"): _names("""
        Ground Black Pepper, Sea Salt, Table Salt, Paprika, Ground Cumin,
        Ground Coriander, Curry Powder, Chilli Powder, Dried Oregano, Mixed Herbs,
        Cinnamon Sticks, Garlic Granules
    """),
    ("Pantry", "Breakfast Cereals"): _names("""
        Corn Flakes, Wheat Biscuit Cereal, Porridge Oats, Bran Flakes, Rice Pops Cereal,
        Muesli, Granola, Honey Loop Cereal, Shredded Wheat, Fruit & Fibre Flakes,
        Crunchy Nut Clusters, Oat Bran Flakes
    """),
    ("Beverages", "Water"): _names("""
        Still Water, Sparkling Water, Spring Water, Mineral Water,
        Flavoured Sparkling Water, Lemon Sparkling Water, Spring Water Bottle,
        Kids Water, Cucumber & Mint Water, Distilled Water,
        Sparkling Water Multipack, Highland Still Water
    """),
    ("Beverages", "Soft Drinks"): _names("""
        Cola, Diet Cola, Lemonade, Orangeade, Ginger Ale, Tonic Water, Soda Water,
        Cream Soda, Root Beer, Cherry Cola, Zero Sugar Cola, Mixed Fruit Cordial
    """),
    ("Beverages", "Juice"): _names("""
        Orange Juice, Apple Juice, Cranberry Juice, Pineapple Juice, Mango Juice,
        Tomato Juice, Grapefruit Juice, Not From Concentrate Orange Juice,
        Smooth Orange Juice, Apple & Mango Juice, Pomegranate Juice, Lemon Juice
    """),
    ("Beverages", "Coffee"): _names("""
        Ground Coffee, Instant Coffee, Coffee Beans, Decaffeinated Coffee,
        Espresso Roast Coffee, Colombian Ground Coffee, Filter Coffee, Coffee Pods,
        Rich Roast Instant Coffee, Americano Coffee Bags, Single Origin Coffee Beans,
        Fairtrade Ground Coffee
    """),
    ("Beverages", "Tea"): _names("""
        English Breakfast Tea, Green Tea, Peppermint Tea, Chamomile Tea, Earl Grey Tea,
        Rooibos Tea, Fruit Tea Selection, Decaffeinated Tea, Herbal Tea Bags,
        Lemon Green Tea, Chai Tea, Loose Leaf Tea
    """),
    ("Beverages", "Beer & Cider"): _names("""
        Lager, Pale Ale, India Pale Ale, Stout, Cider, Fruit Cider, Alcohol Free Lager,
        Craft Lager, Bitter, Pilsner, Golden Ale, Apple Cider
    """),
    ("Beverages", "Wine"): _names("""
        Red Wine, White Wine, Rose Wine, Prosecco, Cava, Malbec Red Wine,
        Sauvignon Blanc, Merlot, Pinot Grigio, Chardonnay, Rioja,
        Alcohol Free Sparkling Wine
    """),
    ("Snacks", "Crisps & Chips"): _names("""
        Ready Salted Crisps, Cheese & Onion Crisps, Salt & Vinegar Crisps,
        Prawn Cocktail Crisps, Tortilla Chips, Salted Tortilla Chips, Cheese Puffs,
        Kettle Cooked Crisps, Multipack Crisps, Ridge Cut Crisps, Sour Cream Crisps,
        Vegetable Crisps
    """),
    ("Snacks", "Chocolate"): _names("""
        Milk Chocolate Bar, Dark Chocolate Bar, White Chocolate Bar, Chocolate Buttons,
        Chocolate Orange, Caramel Chocolate Bar, Nut Chocolate Bar, Chocolate Coins,
        Chocolate Truffles, Chocolate Sharing Bag, Mint Chocolate Bar,
        Fruit & Nut Chocolate
    """),
    ("Snacks", "Biscuits"): _names("""
        Digestive Biscuits, Rich Tea Biscuits, Shortbread Fingers, Chocolate Chip Cookies,
        Custard Creams, Bourbon Biscuits, Orange Jelly Cakes, Ginger Nuts,
        Oat Crunch Biscuits, Wafer Biscuits, Cream Sandwich Biscuits,
        Chocolate Digestives
    """),
    ("Snacks", "Nuts & Seeds"): _names("""
        Salted Peanuts, Dry Roasted Peanuts, Cashew Nuts, Almonds, Mixed Nuts,
        Pistachios, Walnuts, Sunflower Seeds, Pumpkin Seeds, Trail Mix,
        Honey Roasted Cashews, Popcorn Kernels
    """),
    ("Snacks", "Sweets & Candy"): _names("""
        Jelly Babies, Wine Gums, Fruit Pastilles, Liquorice Allsorts, Chewy Sweets,
        Boiled Sweets, Fizzy Cola Bottles, Marshmallows, Toffee Chews, Sour Sweets,
        Sherbet Fountains, Mint Imperials
    """),
    ("Snacks", "Crackers & Rice Cakes"): _names("""
        Cream Crackers, Salted Crackers, Wholegrain Crackers, Rice Cakes, Corn Cakes,
        Oatcakes, Water Biscuits, Cheese Oatcakes, Seeded Crackers, Rye Crispbread,
        Thins Crackers, Puffed Corn Cakes
    """),
    ("Snacks", "Popcorn"): _names("""
        Salted Popcorn, Sweet Popcorn, Toffee Popcorn, Butter Popcorn,
        Sweet & Salty Popcorn, Microwave Popcorn, Sharing Popcorn Bag, Caramel Popcorn,
        Cheese Popcorn, Light Popcorn, Cinema Style Popcorn, Popcorn Multipack
    """),
    ("Household", "Laundry"): _names("""
        Laundry Liquid, Laundry Powder, Biological Laundry Liquid,
        Non Biological Washing Powder, Fabric Conditioner, Laundry Pods,
        Colour Care Liquid, Stain Remover Spray, Delicates Wash Liquid,
        Laundry Capsules, Wool Wash Liquid, Whitening Powder
    """),
    ("Household", "Cleaning Products"): _names("""
        Multi Surface Spray, Bathroom Cleaner, Kitchen Degreaser, Bleach, Floor Cleaner,
        Glass Cleaner, Toilet Cleaner, Anti Bacterial Wipes, Mould Remover, Oven Cleaner,
        Limescale Remover, Disinfectant Spray
    """),
    ("Household", "Kitchen Roll & Tissues"): _names("""
        Kitchen Roll, Kitchen Towel 2 Pack, Toilet Roll, Toilet Tissue 9 Pack,
        Facial Tissues, Pocket Tissues, Recycled Toilet Roll, Luxury Kitchen Roll,
        Paper Napkins, Blue Roll, Extra Soft Toilet Tissue, Tissue Cube
    """),
    ("Household", "Dishwashing"): _names("""
        Washing Up Liquid, Lemon Washing Up Liquid, Dishwasher Tablets, Dishwasher Salt,
        Rinse Aid, Dishwasher Powder, Dishwasher Capsules,
        Antibacterial Washing Up Liquid, Sponge Scourers, Washing Up Brush,
        Dishwasher Cleaner, Eco Dishwasher Tablets
    """),
    ("Household", "Air Fresheners"): _names("""
        Air Freshener Spray, Reed Diffuser, Room Spray, Plug In Air Freshener Refill,
        Car Air Freshener, Scented Candles, Fabric Freshener, Bathroom Air Freshener,
        Air Freshener Gel, Essential Oil Diffuser, Linen Mist, Odour Neutraliser
    """),
    ("Household", "Bin Liners"): _names("""
        Bin Liners, Swing Bin Liners, Pedal Bin Liners, Drawstring Bin Bags,
        Recycling Bags, Compostable Caddy Liners, Heavy Duty Bin Bags, Small Bin Liners,
        Scented Bin Liners, Caddy Refill Bags, Wheelie Bin Bags, Compost Bags
    """),
    ("Personal Care", "Hair Care"): _names("""
        Shampoo, Conditioner, Anti Dandruff Shampoo, Dry Shampoo, Hair Mask,
        Leave In Conditioner, Styling Gel, Hairspray, Argan Oil Shampoo,
        Volumising Shampoo, Kids Shampoo, Colour Protect Conditioner
    """),
    ("Personal Care", "Skin Care"): _names("""
        Face Wash, Moisturising Cream, Day Cream SPF15, Night Cream, Hand Cream,
        Body Lotion, Face Serum, Micellar Water, Toner, Eye Cream, Sunscreen SPF30,
        Cleansing Wipes
    """),
    ("Personal Care", "Oral Care"): _names("""
        Toothpaste, Whitening Toothpaste, Sensitive Toothpaste, Kids Toothpaste,
        Toothbrush, Electric Toothbrush Heads, Mouthwash, Dental Floss,
        Interdental Brushes, Tongue Cleaner, Fluoride Mouthwash, Bamboo Toothbrush
    """),
    ("Personal Care", "Bath & Shower"): _names("""
        Shower Gel, Body Wash, Bath Soak, Bubble Bath, Bar Soap, Hand Wash,
        Antibacterial Hand Wash, Bath Bombs, Shower Cream, Exfoliating Scrub,
        Foam Bath, Liquid Soap Refill
    """),
    ("Personal Care", "Deodorants"): _names("""
        Roll On Deodorant, Aerosol Deodorant, Antiperspirant Spray, Stick Deodorant,
        Sensitive Deodorant, Aluminium Free Deodorant, Mens Deodorant,
        Womens Deodorant, Compressed Deodorant, Sport Deodorant,
        Natural Deodorant Cream, Deodorant Wipes
    """),
    ("Personal Care", "Shaving"): _names("""
        Shaving Gel, Shaving Foam, Razor Blades, Disposable Razors, Mens Razor,
        Shaving Cream, Aftershave Balm, Pre Shave Oil, Razor Cartridges, Womens Razors,
        Post Shave Lotion, Shaving Brush
    """),
    ("Personal Care", "Menstrual Care"): _names("""
        Sanitary Towels, Ultra Thin Pads, Night Pads, Tampons, Applicator Tampons,
        Menstrual Cup, Period Pants, Panty Liners, Scented Pads, Super Plus Tampons,
        Maternity Pads, Reusable Cloth Pads
    """),
    ("Baby", "Baby Food"): _names("""
        Baby Rice, Stage 1 Baby Puree, Stage 2 Baby Puree, Organic Baby Puree,
        Baby Porridge, Baby Pasta Bake, Baby Fruit Pots, Baby Yogurt, Toddler Meal Tray,
        Baby Finger Food, Baby Juice, Organic Baby Muesli
    """),
    ("Baby", "Baby Formula"): _names("""
        First Infant Milk Formula, Follow On Milk Formula, Growing Up Milk,
        Stage 1 Formula, Stage 2 Formula, Comfort Formula, Anti Reflux Formula,
        Hypoallergenic Formula, Ready To Feed Formula, Organic Infant Formula,
        Toddler Milk Drink, Hungry Baby Formula
    """),
    ("Baby", "Nappies"): _names("""
        Newborn Nappies, Size 1 Nappies, Size 2 Nappies, Size 3 Nappies, Size 4 Nappies,
        Size 5 Nappies, Pull Up Pants, Night Time Nappies, Ultra Dry Nappies, Eco Nappies,
        Swimming Nappies, Nappy Pants Size 6
    """),
    ("Baby", "Baby Wipes"): _names("""
        Baby Wipes, Fragrance Free Baby Wipes, Sensitive Baby Wipes, Water Wipes,
        Biodegradable Baby Wipes, Newborn Wipes, Antibacterial Baby Wipes,
        Cotton Soft Wipes, Aloe Vera Baby Wipes, Compact Baby Wipes, Bulk Baby Wipes,
        Travel Baby Wipes
    """),
    ("Baby", "Baby Bath & Care"): _names("""
        Baby Shampoo, Baby Bath, Baby Lotion, Baby Oil, Nappy Cream, Baby Powder,
        Baby Bubble Bath, Baby Massage Oil, Baby Sun Cream, Baby Toothpaste,
        Baby Bath Thermometer, Baby Hair Brush
    """),
    ("Baby", "Baby Snacks"): _names("""
        Baby Rice Cakes, Baby Puffs, Toddler Fruit Snacks, Baby Biscuits,
        Organic Baby Wafers, Baby Crisps, Toddler Cereal Bars, Baby Yogurt Melts,
        Baby Rusks, Toddler Oat Bars, Baby Fruit Puree Pouch, Baby Cheese Bites
    """),
    ("Deli", "Cooked Meats"): _names("""
        Cooked Ham, Honey Roast Ham, Smoked Ham, Chicken Breast Slices,
        Turkey Breast Slices, Roast Beef Slices, Corned Beef, Salami, Pepperoni Slices,
        Parma Ham, Chorizo Slices, Cooked Chicken Pieces
    """),
    ("Deli", "Olives & Antipasti"): _names("""
        Green Olives, Black Olives, Kalamata Olives, Stuffed Olives, Sun Dried Tomatoes,
        Roasted Peppers, Artichoke Hearts, Marinated Feta, Grilled Aubergine,
        Pickled Onions, Caper Berries, Antipasti Mix
    """),
    ("Deli", "Cheese Counter"): _names("""
        Manchego, Gorgonzola, Blue Stilton, Roquefort, Gruyere, Comte, Taleggio,
        Pecorino, Stilton Wedge, Goats Cheese Log, Truffled Brie, Smoked Cheddar
    """),
    ("Deli", "Fresh Pasta & Sauces"): _names("""
        Fresh Tagliatelle, Fresh Ravioli, Fresh Tortelloni, Fresh Gnocchi, Pesto Sauce,
        Arrabbiata Sauce, Carbonara Sauce, Bolognese Sauce, Truffle Pasta Sauce,
        Fresh Lasagne Sheets, Spinach Ricotta Ravioli, Basil Pesto
    """),
    ("Deli", "Dips & Spreads"): _names("""
        Hummus, Red Pepper Hummus, Tzatziki, Taramasalata, Guacamole, Salsa,
        Sour Cream Dip, Baba Ganoush, Pate, Chicken Liver Pate, Olive Tapenade,
        Cheese Dip
    """),
    ("Deli", "Sushi & Ready to Eat"): _names("""
        Salmon Nigiri, Tuna Sushi Pack, California Rolls, Vegetable Sushi,
        Sushi Selection Box, Prawn Tempura Rolls, Chicken Katsu Wrap, Greek Salad Bowl,
        Chicken Caesar Salad, Poke Bowl, Falafel Wrap, Quinoa Salad Pot
    """),
}

# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------

#: Invented "national" brands, one list per category so pairings stay plausible.
NATIONAL_BRANDS: tuple[str, ...] = tuple(
    dict.fromkeys(b for cat in CATEGORIES for b in cat.brands)
)

#: Retailer private-label lines that suit any aisle.
GENERIC_HOUSE_BRANDS: tuple[str, ...] = _names("""
    Laya Basics, Laya Value, Laya Everyday, Laya Essentials, Laya Finest,
    Laya Signature, Laya Reserve, Laya Organic, Laya Free From, Laya Fresh,
    Laya Market, Laya Daily, Laya Handpicked, Basket & Co, Crownfield,
    Everyday Essentials, Poundwise, Northgate Own, Market Lane, Thrift & Co,
    Vale Own Brand, Sunnybank Basics, Copperfield Own, Corner Store Co,
    Homestead Value, Wisebuy, Aldergrove, Bramble & Co, Fernway Essentials,
    Grange Value, Ironbridge Basics, Kingsway Own, Lowfield Value
""")

#: Retailer private-label lines that only make sense in one category - a
#: "Laya Bakery" melon would be nonsense, so these stay in their own aisle.
CATEGORY_HOUSE_BRANDS: dict[str, tuple[str, ...]] = {
    "Produce": _names("Laya Garden, Laya Harvest, Green Basket"),
    "Dairy & Eggs": _names("Meadowgate Essentials, Laya Creamery"),
    "Bakery": _names("Laya Bakery, Hearth & Home"),
    "Meat & Seafood": _names("Laya Butcher, Laya Fishmonger"),
    "Frozen": _names("Laya Frozen, Frostvale"),
    "Pantry": _names("Laya Pantry, Laya Kitchen, Copperfield Pantry"),
    "Beverages": _names("Laya Cellar, Laya Thirst"),
    "Snacks": _names("Laya Snacks, Snackworth"),
    "Household": _names("Laya Home, Hartley Home, Laya Clean"),
    "Personal Care": _names("Laya Care, Laya Beauty"),
    "Baby": _names("Laya Baby, Laya Little Ones"),
    "Deli": _names("Laya Deli, Laya Counter"),
}

#: Every private-label line: the generic ones plus each category's own.
HOUSE_BRANDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        list(GENERIC_HOUSE_BRANDS)
        + [b for cat in CATEGORIES for b in CATEGORY_HOUSE_BRANDS[cat.name]]
    )
)

ALL_BRANDS: tuple[str, ...] = tuple(sorted(set(NATIONAL_BRANDS) | set(HOUSE_BRANDS)))


def house_brands(category: str) -> tuple[str, ...]:
    """Private-label lines that may carry a product in ``category``."""
    return GENERIC_HOUSE_BRANDS + CATEGORY_HOUSE_BRANDS[category]


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Format:
    """A store format: sampling weight, demand factor and size band."""

    name: str
    weight: float
    demand: float
    size_sqm: tuple[int, int]


FORMAT_SPECS: tuple[Format, ...] = (
    Format("convenience", 0.30, 0.55, (120, 450)),
    Format("supermarket", 0.34, 1.00, (900, 2600)),
    Format("discounter", 0.18, 0.90, (800, 1600)),
    Format("hypermarket", 0.08, 1.60, (4000, 9500)),
    Format("online", 0.10, 1.20, (3000, 12000)),
)

FORMAT_BY_NAME: dict[str, Format] = {f.name: f for f in FORMAT_SPECS}

#: region -> (population-ish weight, cities)
REGIONS: dict[str, tuple[float, tuple[str, ...]]] = {
    "Greater London": (0.16, ("Camden", "Croydon", "Ealing", "Hackney", "Islington",
                              "Wandsworth", "Greenwich")),
    "South East": (0.15, ("Brighton", "Guildford", "Milton Keynes", "Oxford",
                          "Portsmouth", "Reading", "Southampton")),
    "South West": (0.09, ("Bath", "Bournemouth", "Bristol", "Exeter", "Plymouth",
                          "Swindon", "Truro")),
    "East of England": (0.10, ("Cambridge", "Chelmsford", "Colchester", "Ipswich",
                               "Luton", "Norwich", "Peterborough")),
    "East Midlands": (0.08, ("Derby", "Leicester", "Lincoln", "Northampton",
                             "Nottingham", "Chesterfield")),
    "West Midlands": (0.10, ("Birmingham", "Coventry", "Stoke-on-Trent", "Telford",
                             "Walsall", "Worcester")),
    "Yorkshire & Humber": (0.09, ("Bradford", "Doncaster", "Hull", "Leeds",
                                  "Sheffield", "York")),
    "North West": (0.12, ("Bolton", "Chester", "Liverpool", "Manchester", "Preston",
                          "Stockport", "Warrington")),
    "North East": (0.05, ("Durham", "Gateshead", "Middlesbrough", "Newcastle",
                          "Sunderland")),
    "Scotland": (0.09, ("Aberdeen", "Dundee", "Edinburgh", "Glasgow", "Inverness",
                        "Stirling")),
    "Wales": (0.05, ("Cardiff", "Newport", "Swansea", "Wrexham", "Bangor")),
}

#: Street-ish suffixes used to make store names unique and realistic.
STORE_SUFFIXES: tuple[str, ...] = (
    "High Street", "Market Square", "Riverside", "Station Road", "The Parade",
    "Green Lane", "Mill Road", "Church Street", "Park View", "Victoria Road",
    "Bridge Street", "Queensway", "Kings Walk", "Northgate", "Old Market",
)


def _weighted(rng: random.Random, population, weights):
    return rng.choices(population, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def categories() -> tuple[str, ...]:
    """Category names, in catalog order."""
    return tuple(c.name for c in CATEGORIES)


def subcategories(category: str) -> tuple[str, ...]:
    """Subcategory names of one category."""
    return CATEGORY_BY_NAME[category].subcategories


def product_names(category: str, subcategory: str) -> tuple[str, ...]:
    """Item-name pool for one (category, subcategory) pair."""
    return PRODUCT_NAMES[(category, subcategory)]


def formats() -> tuple[str, ...]:
    """Store format names."""
    return tuple(f.name for f in FORMAT_SPECS)


def regions() -> tuple[str, ...]:
    """Region names."""
    return tuple(REGIONS)


def cities() -> tuple[str, ...]:
    """Every city in the catalog, sorted."""
    return tuple(sorted(c for _, (_, cs) in REGIONS.items() for c in cs))


def brands() -> tuple[str, ...]:
    """Every distinct brand (national + house), sorted."""
    return ALL_BRANDS


def _pack_label(pack_size: float, uom: str) -> str:
    if uom == "kg":
        return f"{pack_size:g}kg"
    if uom == "litre":
        return f"{pack_size:g}L"
    if uom == "pack":
        return f"{int(pack_size)} pack"
    return "single"


def _pack_factor(pack_size: float, uom: str) -> float:
    """Mild bulk premium so multipacks cost a little more than singles."""
    if pack_size <= 1 or uom == "kg":
        return 1.0
    return min(1.0 + 0.12 * math.log2(pack_size), 1.4)


def build_stores(rng: random.Random, count: int) -> list[dict]:
    """Build ``count`` stores with ids ``1..count``, deterministic in ``rng``."""
    region_names = tuple(REGIONS)
    region_weights = [REGIONS[r][0] for r in region_names]
    format_weights = [f.weight for f in FORMAT_SPECS]
    used: set[str] = set()
    stores: list[dict] = []
    for store_id in range(1, count + 1):
        region = _weighted(rng, region_names, region_weights)
        city = rng.choice(REGIONS[region][1])
        fmt = _weighted(rng, FORMAT_SPECS, format_weights)
        if fmt.name == "online":
            name = f"Laya Online {city}"
        else:
            name = f"Laya {city} {rng.choice(STORE_SUFFIXES)}"
        if name in used:
            name = f"{name} {store_id}"
        used.add(name)
        days_back = int(rng.triangular(0, 10000, 6000))
        stores.append({
            "store_id": store_id,
            "name": name,
            "region": region,
            "city": city,
            "format": fmt.name,
            "size_sqm": rng.randint(*fmt.size_sqm),
            "opened_on": STORE_EPOCH - timedelta(days=days_back),
        })
    return stores


def build_products(rng: random.Random, count: int) -> list[dict]:
    """Build ``count`` products with ids ``1..count`` and ``SKU-%06d`` skus.

    Only catalog-level attributes are decided here; the demand model (base
    units, cost ratio, elasticity, stock cover) is sampled by ``generate.py``.
    """
    cat_names = [c.name for c in CATEGORIES]
    cat_weights = [c.weight for c in CATEGORIES]
    products: list[dict] = []
    for product_id in range(1, count + 1):
        cat = CATEGORY_BY_NAME[_weighted(rng, cat_names, cat_weights)]
        subcategory = rng.choice(cat.subcategories)
        pool = PRODUCT_NAMES[(cat.name, subcategory)]
        base = rng.choice(pool)
        is_private_label = rng.random() < PRIVATE_LABEL_SHARE
        brand = rng.choice(house_brands(cat.name) if is_private_label else cat.brands)

        uom_options = SUBCATEGORY_UOM[subcategory]
        uom = _weighted(rng, tuple(uom_options), [uom_options[u] for u in uom_options])
        pack_size = rng.choice(PACK_SIZES[uom])

        parts = [brand, base]
        if rng.random() < 0.22:
            modifier = rng.choice(modifiers(cat.name, subcategory))
            if modifier.lower() not in base.lower():
                parts.insert(1, modifier)
        if rng.random() < 0.5:
            parts.append(_pack_label(pack_size, uom))

        low, high = cat.price_range
        price = (low + (high - low) * rng.random()) * _pack_factor(pack_size, uom)
        price = round(min(max(price, MIN_PRICE), MAX_PRICE), 2)

        products.append({
            "product_id": product_id,
            "sku": SKU_FORMAT % product_id,
            "name": " ".join(parts),
            "brand": brand,
            "category": cat.name,
            "subcategory": subcategory,
            "uom": uom,
            "pack_size": float(pack_size),
            "is_private_label": is_private_label,
            "is_perishable": rng.random() < cat.perishable_p,
            "list_price": price,
        })
    return products
