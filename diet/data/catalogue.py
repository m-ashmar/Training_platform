"""The food catalogue, as curated content rather than an API import.

Why this file exists
--------------------
The catalogue was 327 rows across six `api_id` prefixes: 124 from Edamam, 100 from a seed
command, 44 that persistence created by itself, 30 custom and 22 from tests. Twenty-three
names were duplicated. Ten rows were Levantine and three of those were corrupt. None of it
was reproducible from a clean checkout, because no import command existed.

An API is a source of NUMBERS. It is not a source of a catalogue. The engine needs four
things per food that no nutrition API returns:

  * a household unit with a minimum and maximum, so a portion is servable
  * a role, so a condiment cannot anchor a meal
  * meal slots, so it knows a food is breakfast food
  * an Arabic name

So this file is the source of truth and USDA is a source of numbers for it. Unit and role
are derived by `seed_food_units`, which already covers the catalogue; `meal_slots` and
`name_ar` are here because only a person can supply them.

Sources
-------
`USDA` entries carry a FoodData Central search term. `build_food_catalogue` resolves each
one to an SR Legacy or Foundation row and caches the per-100g macros.

`LEVANTINE` entries carry their macros inline, because USDA has none of these foods.
Verified absent from SR Legacy and Foundation: labneh, freekeh, halloumi, molokhia, pita
bread, hummus, kishk, jameed, makdous. The values below are per 100 g from standard
composition tables and product labels, and they are the rows a dietitian should review
first.
"""
from __future__ import annotations

# --- meal slot vocabulary -------------------------------------------------
B, L, D, S = "Breakfast", "Lunch", "Dinner", "Snack"
ALL = (B, L, D, S)
MAIN = (L, D)
MORNING = (B, S)

# (canonical name, USDA source, category, meal slots, Arabic name)
#
# The source is a search term, or "fdc:<id>" to pin one FoodData Central row by identity.
# A search is a heuristic and it picked the wrong food nine times out of ninety-eight:
# "chicken breast roasted skinless" returned oven-roasted lunchmeat roll at 14.6 g protein,
# and "chicken thigh roasted skinless" returned chicken SKIN at 44 g fat. Anything whose
# value matters is pinned.
USDA = [
    # --- poultry and meat --------------------------------------------------
    ("Chicken Breast (Grilled)", "fdc:171477", "Proteins", MAIN, "صدر دجاج"),
    ("Chicken Thigh (Skinless)", "fdc:172388", "Proteins", MAIN, "فخذ دجاج"),
    ("Turkey Breast", "turkey breast roasted", "Proteins", MAIN, "صدر حبش"),
    ("Lean Beef Sirloin", "fdc:168636", "Proteins", MAIN, "لحم بقر"),
    ("Beef Mince (Lean)", "ground beef 90% lean cooked", "Proteins", MAIN, "لحمة مفرومة"),
    ("Lamb Leg (Lean)", "fdc:174314", "Proteins", MAIN, "لحم غنم"),
    ("Lamb Shoulder", "fdc:173814", "Proteins", MAIN, "كتف غنم"),
    ("Veal Cutlet", "veal cutlet cooked", "Proteins", MAIN, "لحم عجل"),
    ("Liver (Lamb)", "lamb liver cooked", "Proteins", MAIN, "كبدة"),
    # --- fish and seafood --------------------------------------------------
    ("Salmon Fillet", "salmon atlantic farmed cooked", "Proteins", MAIN, "سلمون"),
    ("Tuna (Fresh)", "tuna yellowfin cooked", "Proteins", MAIN, "تونة"),
    ("Tuna (Canned in Water)", "tuna light canned in water drained", "Proteins", (L, S), "تونة معلبة"),
    ("Cod Fillet", "cod atlantic cooked", "Proteins", MAIN, "سمك قد"),
    ("Sea Bass", "sea bass cooked", "Proteins", MAIN, "قاروص"),
    ("Sardines (Canned)", "sardines canned in oil drained", "Proteins", (L, S), "سردين"),
    ("Shrimp", "shrimp cooked", "Proteins", MAIN, "قريدس"),
    ("Tilapia", "tilapia cooked", "Proteins", MAIN, "بلطي"),
    # --- eggs and dairy ----------------------------------------------------
    ("Egg (Whole)", "egg whole cooked hard boiled", "Proteins", (B, S), "بيض"),
    ("Egg White", "egg white raw", "Proteins", (B, S), "بياض بيض"),
    ("Greek Yogurt (Non-Fat)", "yogurt greek plain nonfat", "Proteins", (B, S), "لبن يوناني"),
    ("Plain Yogurt", "yogurt plain whole milk", "Proteins", ALL, "لبن"),
    ("Cottage Cheese (Low-Fat)", "cottage cheese lowfat 2%", "Proteins", (B, S), "جبنة قريش"),
    ("Feta Cheese", "cheese feta", "Proteins", ALL, "جبنة بيضاء"),
    ("Mozzarella (Part-Skim)", "cheese mozzarella part skim", "Proteins", ALL, "موزاريلا"),
    ("Whole Milk", "milk whole 3.25%", "Proteins", (B, S), "حليب"),
    ("Skim Milk", "milk nonfat fluid", "Proteins", (B, S), "حليب خالي الدسم"),
    # --- plant protein -----------------------------------------------------
    ("Lentils (Cooked)", "lentils cooked boiled", "Proteins", MAIN, "عدس"),
    ("Chickpeas (Cooked)", "chickpeas cooked boiled", "Proteins", MAIN, "حمص حب"),
    ("White Beans (Cooked)", "beans white cooked boiled", "Proteins", MAIN, "فاصولياء بيضاء"),
    ("Kidney Beans (Cooked)", "beans kidney red cooked boiled", "Proteins", MAIN, "فاصولياء حمراء"),
    ("Broad Beans (Cooked)", "fava beans cooked boiled", "Proteins", (B, L), "فول"),
    ("Green Peas (Cooked)", "peas green cooked boiled", "Vegetables", MAIN, "بازلاء"),
    ("Tofu (Firm)", "tofu firm prepared", "Proteins", MAIN, "توفو"),
    # --- grains and starches ----------------------------------------------
    ("White Rice (Cooked)", "fdc:169757", "Carbs", MAIN, "رز أبيض"),
    ("Brown Rice (Cooked)", "rice brown long grain cooked", "Carbs", MAIN, "رز أسمر"),
    ("Bulgur (Cooked)", "bulgur cooked", "Carbs", MAIN, "برغل"),
    ("Oats (Rolled, Dry)", "fdc:173904", "Carbs", MORNING, "شوفان"),
    ("Quinoa (Cooked)", "quinoa cooked", "Carbs", MAIN, "كينوا"),
    ("Barley (Cooked)", "barley pearled cooked", "Carbs", MAIN, "شعير"),
    ("Couscous (Cooked)", "couscous cooked", "Carbs", MAIN, "كسكس"),
    ("Pasta (Cooked)", "fdc:169737", "Carbs", MAIN, "معكرونة"),
    ("Whole Wheat Bread", "bread whole wheat commercially prepared", "Carbs", ALL, "خبز أسمر"),
    ("White Potato (Baked)", "potato baked flesh and skin", "Carbs", MAIN, "بطاطا"),
    ("Sweet Potato (Baked)", "sweet potato baked in skin", "Carbs", MAIN, "بطاطا حلوة"),
    ("Corn (Cooked)", "corn sweet yellow cooked boiled", "Carbs", MAIN, "ذرة"),
    # --- fats, nuts, seeds -------------------------------------------------
    ("Extra Virgin Olive Oil", "oil olive salad or cooking", "Fats", ALL, "زيت زيتون"),
    ("Butter (Salted)", "butter salted", "Fats", (B, S), "زبدة"),
    ("Almonds", "nuts almonds", "Fats", (B, S), "لوز"),
    ("Walnuts", "nuts walnuts english", "Fats", (B, S), "جوز"),
    ("Cashews", "nuts cashew nuts raw", "Fats", (B, S), "كاجو"),
    ("Pistachios", "nuts pistachio nuts raw", "Fats", (B, S), "فستق حلبي"),
    ("Peanut Butter (Natural)", "peanut butter smooth style without salt", "Fats", MORNING, "زبدة فول سوداني"),
    ("Almond Butter", "almond butter plain without salt", "Fats", MORNING, "زبدة لوز"),
    ("Sunflower Seeds", "seeds sunflower seed kernels dried", "Fats", S, "بزر"),
    ("Pumpkin Seeds", "seeds pumpkin seed kernels dried", "Fats", S, "بزر قرع"),
    ("Sesame Seeds", "seeds sesame seeds whole dried", "Fats", ALL, "سمسم"),
    ("Flax Seeds", "seeds flaxseed", "Fats", MORNING, "بذر كتان"),
    ("Chia Seeds", "seeds chia seeds dried", "Fats", MORNING, "شيا"),
    ("Avocado", "avocados raw all commercial varieties", "Fats", MORNING, "أفوكادو"),
    # --- vegetables --------------------------------------------------------
    ("Broccoli", "broccoli cooked boiled drained", "Vegetables", MAIN, "بروكلي"),
    ("Cauliflower", "cauliflower cooked boiled drained", "Vegetables", MAIN, "قرنبيط"),
    ("Spinach", "spinach cooked boiled drained", "Vegetables", ALL, "سبانخ"),
    ("Tomato", "tomatoes red ripe raw", "Vegetables", ALL, "بندورة"),
    ("Cucumber", "fdc:168409", "Vegetables", ALL, "خيار"),
    ("Lettuce (Romaine)", "lettuce cos or romaine raw", "Vegetables", ALL, "خس"),
    ("Carrot", "carrots raw", "Vegetables", ALL, "جزر"),
    ("Bell Pepper (Red)", "peppers sweet red raw", "Vegetables", ALL, "فليفلة"),
    ("Zucchini", "squash summer zucchini cooked boiled", "Vegetables", MAIN, "كوسا"),
    ("Eggplant", "eggplant cooked boiled drained", "Vegetables", MAIN, "باذنجان"),
    ("Okra", "okra cooked boiled drained", "Vegetables", MAIN, "بامية"),
    ("Green Beans", "beans snap green cooked boiled", "Vegetables", MAIN, "فاصولياء خضراء"),
    ("Cabbage", "cabbage cooked boiled drained", "Vegetables", MAIN, "ملفوف"),
    ("Onion", "onions raw", "Vegetables", MAIN, "بصل"),
    ("Garlic", "garlic raw", "Vegetables", MAIN, "ثوم"),
    ("Parsley", "parsley fresh", "Vegetables", ALL, "بقدونس"),
    ("Mint (Fresh)", "peppermint fresh", "Vegetables", ALL, "نعنع"),
    ("Mushrooms (White)", "mushrooms white cooked boiled", "Vegetables", MAIN, "فطر"),
    ("Cauliflower Rice", "cauliflower raw", "Vegetables", MAIN, "أرز القرنبيط"),
    ("Beet", "beets cooked boiled drained", "Vegetables", MAIN, "شمندر"),
    ("Pumpkin", "pumpkin cooked boiled drained", "Vegetables", MAIN, "قرع"),
    ("Celery", "celery raw", "Vegetables", ALL, "كرفس"),
    # --- fruit -------------------------------------------------------------
    ("Apple", "apples raw with skin", "Fruits", MORNING, "تفاح"),
    ("Banana", "bananas raw", "Fruits", MORNING, "موز"),
    ("Orange", "oranges raw navels", "Fruits", MORNING, "برتقال"),
    ("Grapes", "grapes red or green raw", "Fruits", MORNING, "عنب"),
    ("Watermelon", "watermelon raw", "Fruits", S, "بطيخ"),
    ("Melon (Cantaloupe)", "melons cantaloupe raw", "Fruits", S, "شمام"),
    ("Strawberries", "strawberries raw", "Fruits", MORNING, "فراولة"),
    ("Blueberries", "blueberries raw", "Fruits", MORNING, "توت أزرق"),
    ("Peach", "peaches raw", "Fruits", MORNING, "دراق"),
    ("Apricot", "apricots raw", "Fruits", MORNING, "مشمش"),
    ("Fig (Fresh)", "figs raw", "Fruits", MORNING, "تين"),
    ("Pomegranate", "pomegranates raw", "Fruits", MORNING, "رمان"),
    ("Dates (Medjool)", "dates medjool", "Fruits", S, "تمر"),
    ("Kiwi", "kiwifruit green raw", "Fruits", MORNING, "كيوي"),
    ("Mango", "mangos raw", "Fruits", MORNING, "مانجا"),
    ("Pear", "pears raw", "Fruits", MORNING, "إجاص"),
    ("Lemon", "lemons raw without peel", "Fruits", ALL, "ليمون"),
]

# Foods USDA does not carry. Macros are per 100 g. Review these first.
# (name, Arabic, kcal, protein, carbs, fat, category, meal slots)
LEVANTINE = [
    ("Labneh", "لبنة", 174, 8.0, 6.0, 13.0, "Proteins", ALL),
    ("Labneh (Low-Fat)", "لبنة قليلة الدسم", 96, 10.0, 6.5, 3.5, "Proteins", ALL),
    ("Halloumi", "حلوم", 321, 22.0, 2.0, 25.0, "Proteins", ALL),
    ("Akkawi Cheese", "جبنة عكاوي", 264, 18.0, 3.0, 20.0, "Proteins", ALL),
    ("Nabulsi Cheese", "جبنة نابلسية", 290, 20.0, 2.5, 22.0, "Proteins", ALL),
    ("Shanklish", "شنكليش", 315, 21.0, 5.0, 24.0, "Proteins", ALL),
    ("Jameed (Dried Yogurt)", "جميد", 258, 27.0, 30.0, 3.0, "Proteins", MAIN),
    ("Kishk (Dry)", "كشك", 350, 15.0, 60.0, 5.0, "Carbs", B),
    ("Freekeh (Cooked)", "فريكة", 141, 5.7, 28.0, 0.9, "Carbs", MAIN),
    ("Freekeh (Dry)", "فريكة ناشفة", 352, 12.5, 72.0, 2.5, "Carbs", MAIN),
    ("Pita Bread (White)", "خبز عربي", 275, 9.1, 55.7, 1.2, "Carbs", ALL),
    ("Pita Bread (Whole Wheat)", "خبز عربي أسمر", 266, 9.8, 55.0, 2.6, "Carbs", ALL),
    ("Markook Bread", "خبز مرقوق", 290, 9.5, 60.0, 1.5, "Carbs", ALL),
    ("Hummus (Prepared)", "حمص بطحينة", 177, 7.9, 20.1, 8.6, "Proteins", ALL),
    ("Ful Medames (Prepared)", "فول مدمس", 110, 7.6, 16.0, 1.5, "Proteins", (B, L)),
    ("Baba Ghanoush", "بابا غنوج", 150, 3.0, 12.0, 11.0, "Vegetables", ALL),
    ("Tahini", "طحينة", 595, 17.0, 21.2, 53.8, "Fats", ALL),
    ("Molokhia (Cooked)", "ملوخية", 58, 4.0, 7.0, 1.5, "Vegetables", MAIN),
    ("Makdous", "مكدوس", 210, 3.5, 9.0, 18.0, "Vegetables", (B, S)),
    ("Za'atar Mix", "زعتر", 340, 10.0, 45.0, 14.0, "Fats", B),
    ("Sumac", "سماق", 320, 5.0, 60.0, 8.0, "Vegetables", ALL),
    ("Green Olives", "زيتون أخضر", 145, 1.0, 3.8, 15.3, "Fats", ALL),
    ("Black Olives", "زيتون أسود", 115, 0.8, 6.3, 10.7, "Fats", ALL),
    ("Mixed Pickles", "كبيس", 30, 0.6, 6.5, 0.2, "Vegetables", ALL),
    ("Kibbeh (Baked)", "كبة بالصينية", 245, 12.0, 22.0, 12.0, "Proteins", MAIN),
    ("Mujadara", "مجدرة", 160, 5.5, 24.0, 4.5, "Carbs", MAIN),
    ("Tabbouleh", "تبولة", 120, 2.5, 12.0, 7.0, "Vegetables", MAIN),
    ("Fattoush", "فتوش", 110, 2.2, 11.0, 6.5, "Vegetables", MAIN),
    ("Shawarma (Chicken)", "شاورما دجاج", 190, 20.0, 4.0, 10.0, "Proteins", MAIN),
    ("Grape Leaves (Stuffed)", "ورق عنب", 155, 3.0, 20.0, 7.0, "Carbs", MAIN),
    ("Halawa (Tahini Sweet)", "حلاوة طحينية", 540, 12.0, 50.0, 32.0, "Fats", S),
    ("Ashta", "قشطة", 200, 6.0, 10.0, 15.0, "Proteins", S),
    ("Arabic Coffee", "قهوة عربية", 2, 0.1, 0.3, 0.0, "Other", ALL),
    ("Ayran", "عيران", 40, 1.7, 3.0, 2.0, "Proteins", ALL),
    ("Jallab", "جلاب", 120, 0.2, 30.0, 0.1, "Other", S),
]


#: Foods a Levantine client would not call their own. Everything else in `USDA` is
#: universal — chicken, rice, eggs, vegetables, fruit belong to no cuisine. `LEVANTINE`
#: rows are Levantine by construction.
WESTERN = {
    "Oats (Rolled, Dry)", "Quinoa (Cooked)", "Pasta (Cooked)", "Whole Wheat Bread",
    "Peanut Butter (Natural)", "Almond Butter", "Cottage Cheese (Low-Fat)",
    "Greek Yogurt (Non-Fat)", "Mozzarella (Part-Skim)", "Turkey Breast", "Tofu (Firm)",
    "Chia Seeds", "Flax Seeds", "Blueberries", "Kiwi", "Avocado", "Salmon Fillet",
    "Cod Fillet", "Tuna (Canned in Water)", "Sardines (Canned)", "Broccoli", "Kale",
    "Sweet Potato (Baked)", "Corn (Cooked)", "Butter (Salted)",
}


def target_names():
    """Every canonical name the catalogue should contain, in load order."""
    return [row[0] for row in USDA] + [row[0] for row in LEVANTINE]
