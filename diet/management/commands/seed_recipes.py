"""Seed a starter recipe library.

Deliberately Levantine/Mediterranean-leaning: the app serves a Syrian market, and a
meal plan is only followed if the food is food the user recognises. Every dish maps onto
FoodItems already in the catalogue, matched by name, and is skipped when its ingredients
are missing rather than inventing them.

    python manage.py seed_recipes
    python manage.py seed_recipes --replace
"""
from django.core.management.base import BaseCommand
from django.db import transaction

# name, meal types, cuisine, prep minutes, [(food name, grams, scalable)]
#: The dish names a Syrian client reads. The recipe row is translated; the foods
#: carry their own Arabic names, so a substitution or an addition composes in Arabic.
NAMES_AR = {
    'Grilled Chicken with Steamed Vegetables': 'دجاج مشوي مع خضار مسلوقة',
    'Shrimp and Vegetable Saute': 'جمبري مع خضار سوتيه',
    'Turkey Breast Salad': 'سلطة صدر حبش',
    'Tuna and Egg Salad': 'سلطة تونة وبيض',
    'Cottage Cheese and Vegetable Plate': 'صحن جبنة قريش مع خضار',
    'Tilapia with Salad': 'سمك بلطي مع سلطة',
    'Eggs, Halloumi and Pita Breakfast': 'فطور بيض وحلوم مع خبز',
    'Ful with Eggs and Bread': 'فول مع بيض وخبز',
    'Oats with Milk, Banana and Peanut Butter': 'شوفان بالحليب والموز وزبدة الفول السوداني',
    'Cheese Manakish with Egg': 'مناقيش جبنة مع بيض',
    'Labneh, Egg and Avocado Plate': 'صحن لبنة وبيض وأفوكادو',
    'Shakshuka with Feta and Bread': 'شكشوكة بالجبنة مع خبز',
    'Date and Walnut Oats with Milk': 'شوفان بالتمر والجوز مع حليب',
    'Cheese Omelette with Potatoes': 'عجة جبنة مع بطاطا',
    'Peanut Butter Banana Toast with Milk': 'توست زبدة فول سوداني وموز مع حليب',
    'Sardines, Eggs and Bread': 'سردين وبيض وخبز',
    'Boiled Eggs with Tomato and Cucumber': 'بيض مسلوق مع بندورة وخيار',
    'Light Labneh with Cucumber and Bread': 'لبنة خفيفة مع خيار وخبز',
    'Yogurt with Strawberries and Chia': 'لبن بالفراولة وبذور الشيا',
    'Cottage Cheese with Cucumber and Bread': 'جبنة قريش مع خيار وخبز',
    'Skim Milk Oats with Apple': 'شوفان بحليب خالي الدسم مع تفاح',
    'Light Ful with Tomato and Lemon': 'فول خفيف مع بندورة وليمون',
    'Halloumi with Watermelon': 'حلوم مع بطيخ',
    'Grilled Chicken Salad': 'سلطة دجاج مشوي',
    'Tuna Salad with Lemon': 'سلطة تونة بالليمون',
    'Vegetable Soup with Chickpeas': 'شوربة خضار بالحمص',
    'Baked Cod with Vegetables': 'سمك قد مشوي مع خضار',
    'Yogurt Cucumber Salad with Eggs': 'خيار بلبن مع بيض',
    'Molokhia with Rice': 'ملوخية مع أرز',
    'Grilled Chicken with Rice and Greens': 'دجاج مشوي مع أرز وخضار',
    'Chicken and Rice with Spinach': 'دجاج وأرز مع سبانخ',
    'Baked Salmon with Sweet Potato': 'سلمون مشوي مع بطاطا حلوة',
    'Lentil Soup with Greens': 'شوربة عدس بالخضار',
    'Overnight Oats with Banana': 'شوفان منقوع بالموز',
    'Egg White Scramble with Avocado': 'بياض بيض مقلي مع أفوكادو',
    'Yogurt, Apple and Almond Butter': 'لبن وتفاح وزبدة لوز',
    'Chicken and Rice Meal Prep': 'وجبة دجاج وأرز',
    'Banana Almond Snack': 'موز ولوز',
    'Greek Yogurt Protein Bowl': 'طبق لبن يوناني بالبروتين',
    'Salmon Rice Bowl': 'طبق سلمون مع أرز',
    'Mujadara Style Lentils and Rice': 'عدس وأرز على طريقة المجدرة',
    'Avocado Toast with Eggs': 'توست أفوكادو مع بيض',
    'Almond Butter Oat Bowl': 'طبق شوفان بزبدة اللوز',
    'Salmon and Avocado Breakfast': 'فطور سلمون وأفوكادو',
    'Chicken Avocado Rice Bowl': 'طبق دجاج وأفوكادو مع أرز',
    'Ful Medames Plate': 'صحن فول مدمس',
    "Labneh and Za'atar Plate": 'صحن لبنة وزعتر',
    "Za'atar Manakish": 'مناقيش زعتر',
    'Shakshuka': 'شكشوكة',
    'Eggs with Sumac and Pita': 'بيض بالسماق مع خبز',
    'Halloumi, Tomato and Cucumber': 'حلوم وبندورة وخيار',
    'Kishk Breakfast': 'فطور كشك',
    'Cheese and Olive Plate': 'صحن جبنة وزيتون',
    'Labneh with Honey and Walnuts': 'لبنة بالعسل والجوز',
    'Yogurt with Dates and Pistachios': 'لبن بالتمر والفستق الحلبي',
    'Oats with Dates and Tahini': 'شوفان بالتمر والطحينة',
    'Shanklish Salad Breakfast': 'فطور سلطة شنكليش',
    'Egg White Omelette with Vegetables': 'عجة بياض بيض بالخضار',
    'Shish Tawook Plate': 'صحن شيش طاووق',
    'Kafta with Bulgur': 'كفتة مع برغل',
    'Kafta and Potato Tray': 'صينية كفتة وبطاطا',
    'Kibbeh Plate with Salad': 'صحن كبة مع سلطة',
    'Mansaf': 'منسف',
    'Maqluba': 'مقلوبة',
    'Molokhia with Chicken and Rice': 'ملوخية بالدجاج مع أرز',
    'Fasolia with Rice': 'فاصولياء مع أرز',
    'Bamia with Lamb and Rice': 'بامية باللحم مع أرز',
    'Mujadara with Salad': 'مجدرة مع سلطة',
    'Freekeh with Chicken': 'فريكة بالدجاج',
    'Bulgur Pilaf with Lamb': 'برغل مفلفل باللحم',
    'Fatteh with Chickpeas': 'فتة حمص',
    'Chicken Fatteh': 'فتة دجاج',
    'Sayadieh': 'صيادية',
    'Grilled Sea Bass with Tabbouleh': 'سمك قاروص مشوي مع تبولة',
    'Grilled Chicken with Fattoush': 'دجاج مشوي مع فتوش',
    'Lentil Soup with Pita and Lemon': 'شوربة عدس مع خبز وليمون',
    'Rice, Lentils and Chickpeas': 'أرز وعدس وحمص',
    'Stuffed Grape Leaves Plate': 'صحن ورق عنب محشي',
    'Chicken Shawarma Plate': 'صحن شاورما دجاج',
    'Lamb Liver with Onion': 'كبدة غنم بالبصل',
    'Levantine Eggplant Moussaka': 'مسقعة باذنجان',
    'Spinach and Onion with Pita': 'سبانخ بالبصل مع خبز',
    'Green Beans in Olive Oil with Rice': 'فاصولياء خضراء بالزيت مع أرز',
    'Peas with Beef and Rice': 'بازلاء باللحم مع أرز',
    'Cauliflower with Tahini and Rice': 'قرنبيط بالطحينة مع أرز',
    'Chicken with Freekeh and Yogurt': 'دجاج مع فريكة ولبن',
    'Grilled Salmon with Bulgur': 'سلمون مشوي مع برغل',
    'Tilapia with Rice and Salad': 'سمك بلطي مع أرز وسلطة',
    'Veal with Potatoes and Peas': 'لحم عجل مع بطاطا وبازلاء',
    'Tuna, Chickpea and Parsley Salad': 'سلطة تونة وحمص وبقدونس',
    'Pasta with Chicken and Vegetables': 'معكرونة بالدجاج والخضار',
    'Dates and Walnuts': 'تمر وجوز',
    'Labneh and Cucumber': 'لبنة وخيار',
    'Hummus with Pita': 'حمص مع خبز',
    'Ayran and Almonds': 'عيران ولوز',
    'Fruit and Pistachios': 'فواكه وفستق حلبي',
    'Halawa with Bread': 'حلاوة مع خبز',
    'Cheese, Olives and Tomato': 'جبنة وزيتون وبندورة',
    'Pomegranate and Yogurt': 'رمان ولبن',
    'Baba Ghanoush with Pita': 'بابا غنوج مع خبز',
    'Makdous and Bread': 'مكدوس مع خبز',
    'Boiled Egg and Cucumber': 'بيض مسلوق وخيار',
    'Cottage Cheese and Fruit': 'جبنة قريش وفواكه',
    'Carrots and Hummus': 'جزر وحمص',
    'Walnuts and Figs': 'جوز وتين',
}


RECIPES = [
    ("Grilled Chicken with Rice and Greens", ["Lunch", "Dinner"], "Levantine", 25, [
        ("Chicken Breast", 150, True), ("White Rice", 180, True),
        ("Broccoli", 120, True), ("Olive Oil", 8, True)]),
    ("Chicken and Rice with Spinach", ["Lunch", "Dinner"], "Levantine", 35, [
        ("Chicken Breast", 140, True), ("White Rice", 150, True),
        ("Spinach", 80, True), ("Olive Oil", 10, True)]),
    ("Baked Salmon with Sweet Potato", ["Dinner"], "Mediterranean", 30, [
        ("Salmon", 150, True), ("Sweet Potato", 200, True),
        ("Spinach", 100, True), ("Olive Oil", 7, True)]),
    ("Lentil Soup with Greens", ["Lunch", "Dinner"], "Levantine", 40, [
        ("Lentils", 180, True), ("Spinach", 80, True), ("Olive Oil", 8, True)]),
    ("Overnight Oats with Banana", ["Breakfast"], "Everyday", 5, [
        ("Oats", 70, True), ("Greek Yogurt", 150, True), ("Banana", 100, True)]),
    ("Egg White Scramble with Avocado", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 200, True), ("Avocado", 60, True), ("Olive Oil", 5, True)]),
    ("Yogurt, Apple and Almond Butter", ["Breakfast", "Snack"], "Everyday", 5, [
        ("Greek Yogurt", 170, True), ("Apple", 120, True), ("Almond Butter", 15, True)]),
    ("Chicken and Rice Meal Prep", ["Lunch"], "Everyday", 30, [
        ("Chicken Breast", 170, True), ("White Rice", 200, True),
        ("Broccoli", 150, True), ("Olive Oil", 8, True)]),
    ("Banana Almond Snack", ["Snack"], "Everyday", 2, [
        ("Banana", 110, True), ("Almond Butter", 20, True)]),
    ("Greek Yogurt Protein Bowl", ["Snack", "Breakfast"], "Everyday", 3, [
        ("Greek Yogurt", 200, True), ("Oats", 30, True), ("Apple", 80, True)]),
    ("Salmon Rice Bowl", ["Lunch"], "Mediterranean", 25, [
        ("Salmon", 130, True), ("White Rice", 170, True),
        ("Broccoli", 100, True), ("Olive Oil", 6, True)]),
    ("Mujadara Style Lentils and Rice", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Lentils", 150, True), ("White Rice", 120, True), ("Olive Oil", 10, True)]),
    # Higher-fat breakfasts. The first library skewed low-fat, so a 700 kcal breakfast
    # target with ~17 g fat had nothing that fit and fell back to component assembly.
    ("Avocado Toast with Eggs", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 150, True), ("Whole Wheat Bread", 60, True),
        ("Avocado", 90, True), ("Olive Oil", 6, True)]),
    ("Almond Butter Oat Bowl", ["Breakfast"], "Everyday", 6, [
        ("Oats", 80, True), ("Greek Yogurt", 120, True),
        ("Almond Butter", 25, True), ("Banana", 80, True)]),
    ("Salmon and Avocado Breakfast", ["Breakfast"], "Mediterranean", 15, [
        ("Salmon", 110, True), ("Avocado", 70, True),
        ("Spinach", 60, True), ("Olive Oil", 5, True)]),
    ("Chicken Avocado Rice Bowl", ["Lunch", "Dinner"], "Everyday", 25, [
        ("Chicken Breast", 140, True), ("White Rice", 160, True),
        ("Avocado", 70, True), ("Broccoli", 100, True)]),

    # ======================= Levantine library =======================
    # Every line is a catalogue food; grams are one person's plate as a Syrian kitchen
    # serves it. The engine reads meal shapes, food pairings and meal-appropriateness
    # off this list, so every dish here widens all three. Amounts are a first pass for
    # the owner to review; the macros come from the catalogue rows, not from here.

    # --- breakfasts -------------------------------------------------------
    ("Ful Medames Plate", ["Breakfast"], "Levantine", 10, [
        ("Ful Medames (Prepared)", 220, True), ("Pita Bread (White)", 80, True),
        ("Tomato", 80, True), ("Extra Virgin Olive Oil", 10, True), ("Lemon", 20, True)]),
    ("Labneh and Za'atar Plate", ["Breakfast"], "Levantine", 5, [
        ("Labneh", 120, True), ("Pita Bread (White)", 80, True),
        ("Za'atar Mix", 10, True), ("Extra Virgin Olive Oil", 10, True),
        ("Cucumber", 80, True), ("Green Olives", 25, True)]),
    ("Za'atar Manakish", ["Breakfast", "Snack"], "Levantine", 15, [
        ("Markook Bread", 100, True), ("Za'atar Mix", 15, True),
        ("Extra Virgin Olive Oil", 14, True), ("Tomato", 60, True), ("Mint (Fresh)", 5, True)]),
    ("Shakshuka", ["Breakfast", "Dinner"], "Levantine", 20, [
        ("Egg (Whole)", 120, True), ("Tomato", 200, True), ("Onion", 50, True),
        ("Bell Pepper (Red)", 60, True), ("Extra Virgin Olive Oil", 10, True),
        ("Pita Bread (White)", 60, True)]),
    ("Eggs with Sumac and Pita", ["Breakfast"], "Levantine", 10, [
        ("Egg (Whole)", 110, True), ("Pita Bread (White)", 70, True),
        ("Sumac", 3, True), ("Extra Virgin Olive Oil", 8, True), ("Tomato", 80, True)]),
    ("Halloumi, Tomato and Cucumber", ["Breakfast"], "Levantine", 10, [
        ("Halloumi", 90, True), ("Tomato", 100, True), ("Cucumber", 100, True),
        ("Pita Bread (Whole Wheat)", 70, True), ("Extra Virgin Olive Oil", 7, True)]),
    ("Kishk Breakfast", ["Breakfast"], "Levantine", 15, [
        ("Kishk (Dry)", 60, True), ("Onion", 40, True), ("Extra Virgin Olive Oil", 10, True),
        ("Pita Bread (White)", 60, True)]),
    ("Cheese and Olive Plate", ["Breakfast"], "Levantine", 5, [
        ("Akkawi Cheese", 80, True), ("Black Olives", 30, True), ("Tomato", 100, True),
        ("Cucumber", 80, True), ("Pita Bread (White)", 80, True), ("Mint (Fresh)", 5, True)]),
    ("Labneh with Honey and Walnuts", ["Breakfast", "Snack"], "Levantine", 3, [
        ("Labneh", 150, True), ("Honey", 15, True), ("Walnuts", 20, True),
        ("Pita Bread (Whole Wheat)", 50, True)]),
    ("Yogurt with Dates and Pistachios", ["Breakfast", "Snack"], "Levantine", 3, [
        ("Plain Yogurt", 200, True), ("Dates (Medjool)", 40, True), ("Pistachios", 20, True)]),
    ("Oats with Dates and Tahini", ["Breakfast"], "Levantine", 8, [
        ("Oats (Rolled, Dry)", 60, True), ("Whole Milk", 200, True),
        ("Dates (Medjool)", 30, True), ("Tahini", 12, True)]),
    ("Shanklish Salad Breakfast", ["Breakfast"], "Levantine", 8, [
        ("Shanklish", 60, True), ("Tomato", 120, True), ("Onion", 30, True),
        ("Extra Virgin Olive Oil", 10, True), ("Pita Bread (White)", 70, True)]),
    ("Egg White Omelette with Vegetables", ["Breakfast"], "Everyday", 12, [
        ("Egg White", 180, True), ("Spinach", 60, True), ("Mushrooms (White)", 60, True),
        ("Extra Virgin Olive Oil", 7, True), ("Pita Bread (Whole Wheat)", 50, True)]),

    # --- lunches and dinners: grills and plates ---------------------------
    ("Shish Tawook Plate", ["Lunch", "Dinner"], "Levantine", 30, [
        ("Chicken Breast", 160, True), ("White Rice", 180, True), ("Garlic", 6, True),
        ("Tomato", 80, True), ("Cucumber", 80, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Kafta with Bulgur", ["Lunch", "Dinner"], "Levantine", 30, [
        ("Lamb Mince (Lean)", 150, True), ("Parsley", 20, True), ("Onion", 50, True),
        ("Bulgur (Cooked)", 200, True), ("Tomato", 100, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Kafta and Potato Tray", ["Dinner"], "Levantine", 45, [
        ("Beef Mince (Lean)", 150, True), ("White Potato (Baked)", 200, True),
        ("Tomato", 120, True), ("Onion", 50, True), ("Extra Virgin Olive Oil", 10, True)]),
    ("Kibbeh Plate with Salad", ["Lunch", "Dinner"], "Levantine", 20, [
        ("Kibbeh (Baked)", 180, True), ("Plain Yogurt", 120, True),
        ("Cucumber", 80, True), ("Tomato", 80, True), ("Mint (Fresh)", 5, True)]),
    ("Mansaf", ["Lunch", "Dinner"], "Levantine", 60, [
        ("Lamb Leg (Lean)", 150, True), ("White Rice", 200, True),
        ("Jameed (Dried Yogurt)", 30, True), ("Almonds", 15, True), ("Markook Bread", 40, True)]),
    ("Maqluba", ["Lunch", "Dinner"], "Levantine", 60, [
        ("Chicken Thigh (Skinless)", 150, True), ("White Rice", 180, True),
        ("Eggplant", 120, True), ("Cauliflower", 80, True), ("Extra Virgin Olive Oil", 10, True),
        ("Plain Yogurt", 100, True)]),
    ("Molokhia with Chicken and Rice", ["Lunch", "Dinner"], "Levantine", 50, [
        ("Molokhia (Cooked)", 250, True), ("Chicken Thigh (Skinless)", 140, True),
        ("White Rice", 180, True), ("Garlic", 8, True), ("Lemon", 20, True)]),
    ("Fasolia with Rice", ["Lunch", "Dinner"], "Levantine", 45, [
        ("White Beans (Cooked)", 200, True), ("Beef Mince (Lean)", 100, True),
        ("Tomato", 150, True), ("White Rice", 180, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Bamia with Lamb and Rice", ["Lunch", "Dinner"], "Levantine", 50, [
        ("Okra", 200, True), ("Lamb Shoulder", 130, True), ("Tomato", 150, True),
        ("White Rice", 180, True), ("Garlic", 6, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Mujadara with Salad", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Lentils (Cooked)", 180, True), ("White Rice", 120, True), ("Onion", 80, True),
        ("Extra Virgin Olive Oil", 14, True), ("Cucumber", 80, True), ("Tomato", 80, True)]),
    ("Freekeh with Chicken", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Freekeh (Cooked)", 220, True), ("Chicken Thigh (Skinless)", 150, True),
        ("Almonds", 15, True), ("Extra Virgin Olive Oil", 8, True), ("Plain Yogurt", 100, True)]),
    ("Bulgur Pilaf with Lamb", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Bulgur (Cooked)", 220, True), ("Lamb Shoulder", 130, True), ("Onion", 50, True),
        ("Tomato", 80, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Fatteh with Chickpeas", ["Lunch", "Dinner"], "Levantine", 20, [
        ("Chickpeas (Cooked)", 200, True), ("Pita Bread (White)", 60, True),
        ("Plain Yogurt", 150, True), ("Tahini", 15, True), ("Garlic", 4, True),
        ("Almonds", 10, True), ("Extra Virgin Olive Oil", 7, True)]),
    ("Chicken Fatteh", ["Lunch", "Dinner"], "Levantine", 30, [
        ("Chicken Breast", 140, True), ("Pita Bread (White)", 60, True),
        ("Plain Yogurt", 150, True), ("Tahini", 12, True), ("Garlic", 4, True),
        ("Almonds", 10, True), ("White Rice", 100, True)]),
    ("Sayadieh", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Sea Bass", 170, True), ("White Rice", 200, True), ("Onion", 80, True),
        ("Extra Virgin Olive Oil", 12, True), ("Almonds", 10, True), ("Lemon", 20, True)]),
    ("Grilled Sea Bass with Tabbouleh", ["Lunch", "Dinner"], "Levantine", 30, [
        ("Sea Bass", 180, True), ("Tabbouleh", 180, True), ("Pita Bread (White)", 50, True),
        ("Lemon", 20, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Grilled Chicken with Fattoush", ["Lunch", "Dinner"], "Levantine", 30, [
        ("Chicken Breast", 160, True), ("Fattoush", 200, True), ("Hummus (Prepared)", 60, True),
        ("Pita Bread (White)", 50, True)]),
    ("Lentil Soup with Pita and Lemon", ["Lunch", "Dinner"], "Levantine", 40, [
        ("Lentils (Cooked)", 250, True), ("Onion", 50, True), ("Carrot", 60, True),
        ("Extra Virgin Olive Oil", 10, True), ("Pita Bread (White)", 60, True), ("Lemon", 20, True)]),
    ("Rice, Lentils and Chickpeas", ["Lunch", "Dinner"], "Levantine", 40, [
        ("White Rice", 150, True), ("Lentils (Cooked)", 120, True),
        ("Chickpeas (Cooked)", 80, True), ("Onion", 60, True), ("Tomato", 100, True),
        ("Extra Virgin Olive Oil", 10, True)]),
    ("Stuffed Grape Leaves Plate", ["Lunch", "Dinner"], "Levantine", 15, [
        ("Grape Leaves (Stuffed)", 250, True), ("Plain Yogurt", 120, True),
        ("Cucumber", 80, True), ("Lemon", 20, True)]),
    ("Chicken Shawarma Plate", ["Lunch", "Dinner"], "Levantine", 15, [
        ("Shawarma (Chicken)", 180, True), ("Pita Bread (White)", 80, True),
        ("Mixed Pickles", 40, True), ("Tomato", 80, True), ("Garlic", 6, True),
        ("Hummus (Prepared)", 50, True)]),
    ("Lamb Liver with Onion", ["Lunch", "Dinner"], "Levantine", 20, [
        ("Liver (Lamb)", 150, True), ("Onion", 100, True), ("Extra Virgin Olive Oil", 10, True),
        ("Pita Bread (White)", 80, True), ("Lemon", 20, True), ("Parsley", 10, True)]),
    ("Levantine Eggplant Moussaka", ["Lunch", "Dinner"], "Levantine", 45, [
        ("Eggplant", 250, True), ("Chickpeas (Cooked)", 120, True), ("Tomato", 150, True),
        ("Onion", 60, True), ("Extra Virgin Olive Oil", 14, True), ("Pita Bread (White)", 60, True)]),
    ("Spinach and Onion with Pita", ["Lunch", "Dinner"], "Levantine", 25, [
        ("Spinach", 250, True), ("Onion", 80, True), ("Extra Virgin Olive Oil", 12, True),
        ("Lemon", 20, True), ("Pita Bread (Whole Wheat)", 80, True), ("Plain Yogurt", 100, True)]),
    ("Green Beans in Olive Oil with Rice", ["Lunch", "Dinner"], "Levantine", 35, [
        ("Green Beans", 220, True), ("Tomato", 120, True), ("Onion", 50, True),
        ("Extra Virgin Olive Oil", 14, True), ("White Rice", 160, True)]),
    ("Peas with Beef and Rice", ["Lunch", "Dinner"], "Levantine", 40, [
        ("Green Peas (Cooked)", 200, True), ("Beef Mince (Lean)", 110, True),
        ("Tomato", 100, True), ("White Rice", 170, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Cauliflower with Tahini and Rice", ["Lunch", "Dinner"], "Levantine", 35, [
        ("Cauliflower", 250, True), ("Tahini", 20, True), ("Lemon", 20, True),
        ("Garlic", 4, True), ("White Rice", 150, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Chicken with Freekeh and Yogurt", ["Dinner"], "Levantine", 40, [
        ("Chicken Breast", 150, True), ("Freekeh (Cooked)", 180, True),
        ("Plain Yogurt", 120, True), ("Cucumber", 80, True)]),
    ("Grilled Salmon with Bulgur", ["Dinner"], "Mediterranean", 25, [
        ("Salmon Fillet", 150, True), ("Bulgur (Cooked)", 180, True), ("Spinach", 80, True),
        ("Lemon", 20, True), ("Extra Virgin Olive Oil", 7, True)]),
    ("Tilapia with Rice and Salad", ["Lunch", "Dinner"], "Levantine", 25, [
        ("Tilapia", 170, True), ("White Rice", 170, True), ("Tomato", 80, True),
        ("Cucumber", 80, True), ("Lemon", 20, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Veal with Potatoes and Peas", ["Dinner"], "Levantine", 40, [
        ("Veal Cutlet", 150, True), ("White Potato (Baked)", 180, True),
        ("Green Peas (Cooked)", 100, True), ("Extra Virgin Olive Oil", 8, True)]),
    ("Tuna, Chickpea and Parsley Salad", ["Lunch"], "Mediterranean", 10, [
        ("Tuna (Canned in Water)", 120, True), ("Chickpeas (Cooked)", 150, True),
        ("Parsley", 15, True), ("Tomato", 100, True), ("Extra Virgin Olive Oil", 12, True),
        ("Lemon", 20, True), ("Pita Bread (Whole Wheat)", 50, True)]),
    ("Pasta with Chicken and Vegetables", ["Lunch", "Dinner"], "Everyday", 25, [
        ("Pasta (Cooked)", 200, True), ("Chicken Breast", 130, True), ("Tomato", 100, True),
        ("Zucchini", 80, True), ("Extra Virgin Olive Oil", 8, True)]),

    # --- substantial breakfasts: the 3,000+ kcal client's morning ------------
    # Above ~900 kcal one breakfast dish reached the target and was served six days
    # of seven. These are what a Syrian kitchen plates for someone who trains hard.
    ("Eggs, Halloumi and Pita Breakfast", ["Breakfast"], "Levantine", 15, [
        ("Egg (Whole)", 120, True), ("Halloumi", 80, True), ("Pita Bread (White)", 120, True),
        ("Tomato", 80, True), ("Cucumber", 60, True), ("Black Olives", 25, True),
        ("Extra Virgin Olive Oil", 10, True)]),
    ("Ful with Eggs and Bread", ["Breakfast"], "Levantine", 15, [
        ("Ful Medames (Prepared)", 250, True), ("Egg (Whole)", 100, True),
        ("Pita Bread (White)", 120, True), ("Tomato", 80, True), ("Onion", 30, True),
        ("Lemon", 15, True), ("Extra Virgin Olive Oil", 12, True)]),
    ("Oats with Milk, Banana and Peanut Butter", ["Breakfast"], "Everyday", 8, [
        ("Oats (Rolled, Dry)", 90, True), ("Whole Milk", 300, True), ("Banana", 120, True),
        ("Peanut Butter (Natural)", 30, True), ("Honey", 15, True)]),
    ("Cheese Manakish with Egg", ["Breakfast"], "Levantine", 20, [
        ("Pita Bread (White)", 120, True), ("Akkawi Cheese", 80, True), ("Egg (Whole)", 60, True),
        ("Za'atar Mix", 8, True), ("Tomato", 80, True), ("Extra Virgin Olive Oil", 15, True)]),
    ("Labneh, Egg and Avocado Plate", ["Breakfast"], "Levantine", 10, [
        ("Labneh", 120, True), ("Egg (Whole)", 120, True), ("Avocado", 80, True),
        ("Pita Bread (Whole Wheat)", 100, True), ("Mint (Fresh)", 5, True),
        ("Extra Virgin Olive Oil", 8, True)]),
    ("Shakshuka with Feta and Bread", ["Breakfast"], "Levantine", 20, [
        ("Egg (Whole)", 150, True), ("Tomato", 200, True), ("Onion", 50, True),
        ("Bell Pepper (Red)", 60, True), ("Feta Cheese", 50, True),
        ("Pita Bread (White)", 120, True), ("Extra Virgin Olive Oil", 12, True)]),
    ("Date and Walnut Oats with Milk", ["Breakfast"], "Levantine", 8, [
        ("Oats (Rolled, Dry)", 80, True), ("Whole Milk", 250, True), ("Dates (Medjool)", 50, True),
        ("Walnuts", 30, True), ("Tahini", 15, True)]),
    ("Cheese Omelette with Potatoes", ["Breakfast"], "Everyday", 20, [
        ("Egg (Whole)", 150, True), ("Mozzarella (Part-Skim)", 60, True),
        ("White Potato (Baked)", 200, True), ("Onion", 40, True), ("Parsley", 10, True),
        ("Extra Virgin Olive Oil", 12, True)]),
    ("Peanut Butter Banana Toast with Milk", ["Breakfast"], "Everyday", 5, [
        ("Whole Wheat Bread", 90, True), ("Peanut Butter (Natural)", 40, True),
        ("Banana", 120, True), ("Whole Milk", 250, True), ("Honey", 10, True)]),
    ("Sardines, Eggs and Bread", ["Breakfast"], "Levantine", 10, [
        ("Sardines (Canned)", 100, True), ("Egg (Whole)", 100, True),
        ("Pita Bread (Whole Wheat)", 100, True), ("Tomato", 100, True), ("Lemon", 15, True),
        ("Extra Virgin Olive Oil", 8, True)]),
    # --- light breakfasts: the 1,200 kcal client's morning ---------------------
    ("Boiled Eggs with Tomato and Cucumber", ["Breakfast"], "Levantine", 12, [
        ("Egg (Whole)", 100, True), ("Tomato", 100, True), ("Cucumber", 100, True),
        ("Pita Bread (Whole Wheat)", 30, True), ("Mint (Fresh)", 5, True)]),
    ("Light Labneh with Cucumber and Bread", ["Breakfast"], "Levantine", 5, [
        ("Labneh (Low-Fat)", 100, True), ("Cucumber", 100, True),
        ("Pita Bread (Whole Wheat)", 40, True), ("Za'atar Mix", 5, True),
        ("Extra Virgin Olive Oil", 5, True)]),
    ("Yogurt with Strawberries and Chia", ["Breakfast", "Snack"], "Everyday", 3, [
        ("Greek Yogurt (Non-Fat)", 170, True), ("Strawberries", 100, True),
        ("Chia Seeds", 10, True), ("Honey", 8, True)]),
    ("Cottage Cheese with Cucumber and Bread", ["Breakfast"], "Everyday", 5, [
        ("Cottage Cheese (Low-Fat)", 150, True), ("Cucumber", 100, True),
        ("Whole Wheat Bread", 30, True), ("Tomato", 80, True)]),
    ("Skim Milk Oats with Apple", ["Breakfast"], "Everyday", 8, [
        ("Oats (Rolled, Dry)", 40, True), ("Skim Milk", 200, True), ("Apple", 120, True)]),
    ("Light Ful with Tomato and Lemon", ["Breakfast"], "Levantine", 10, [
        ("Ful Medames (Prepared)", 150, True), ("Tomato", 100, True), ("Lemon", 15, True),
        ("Parsley", 10, True), ("Pita Bread (Whole Wheat)", 30, True),
        ("Extra Virgin Olive Oil", 5, True)]),
    ("Halloumi with Watermelon", ["Breakfast", "Snack"], "Levantine", 5, [
        ("Halloumi", 50, True), ("Watermelon", 200, True), ("Mint (Fresh)", 5, True)]),
    # --- light dinners: the 1,200 kcal client's evening ------------------------
    ("Grilled Chicken Salad", ["Lunch", "Dinner"], "Everyday", 15, [
        ("Chicken Breast (Grilled)", 100, True), ("Lettuce (Romaine)", 100, True),
        ("Tomato", 100, True), ("Cucumber", 100, True), ("Lemon", 15, True),
        ("Extra Virgin Olive Oil", 7, True)]),
    ("Tuna Salad with Lemon", ["Lunch", "Dinner"], "Mediterranean", 10, [
        ("Tuna (Canned in Water)", 100, True), ("Lettuce (Romaine)", 80, True),
        ("Tomato", 100, True), ("Cucumber", 80, True), ("Onion", 30, True), ("Lemon", 15, True),
        ("Extra Virgin Olive Oil", 7, True)]),
    ("Vegetable Soup with Chickpeas", ["Lunch", "Dinner"], "Levantine", 30, [
        ("Chickpeas (Cooked)", 100, True), ("Carrot", 80, True), ("Zucchini", 100, True),
        ("Tomato", 100, True), ("Onion", 40, True), ("Celery", 40, True),
        ("Extra Virgin Olive Oil", 7, True)]),
    ("Baked Cod with Vegetables", ["Dinner"], "Mediterranean", 25, [
        ("Cod Fillet", 150, True), ("Zucchini", 100, True), ("Carrot", 80, True),
        ("Lemon", 15, True), ("Extra Virgin Olive Oil", 7, True)]),
    ("Yogurt Cucumber Salad with Eggs", ["Dinner"], "Levantine", 12, [
        ("Plain Yogurt", 150, True), ("Cucumber", 150, True), ("Garlic", 3, True),
        ("Mint (Fresh)", 5, True), ("Egg (Whole)", 100, True)]),
    ("Molokhia with Rice", ["Lunch", "Dinner"], "Levantine", 20, [
        ("Molokhia (Cooked)", 200, True), ("White Rice (Cooked)", 100, True), ("Lemon", 15, True)]),
    # --- protein-dense light dinners: a 1,200 kcal weight-loss client eats ~280 kcal
    # at dinner with 2 g/kg of protein, which is 40% of the plate's energy. Salads and
    # soups do not reach that; lean protein with vegetables does.
    ("Grilled Chicken with Steamed Vegetables", ["Lunch", "Dinner"], "Everyday", 20, [
        ("Chicken Breast (Grilled)", 120, True), ("Broccoli", 100, True), ("Carrot", 80, True),
        ("Lemon", 15, True), ("Extra Virgin Olive Oil", 5, True)]),
    ("Shrimp and Vegetable Saute", ["Dinner"], "Mediterranean", 15, [
        ("Shrimp", 150, True), ("Zucchini", 100, True), ("Bell Pepper (Red)", 80, True),
        ("Garlic", 3, True), ("Extra Virgin Olive Oil", 7, True)]),
    ("Turkey Breast Salad", ["Lunch", "Dinner"], "Everyday", 10, [
        ("Turkey Breast", 120, True), ("Lettuce (Romaine)", 100, True), ("Tomato", 100, True),
        ("Cucumber", 80, True), ("Lemon", 15, True), ("Extra Virgin Olive Oil", 5, True)]),
    ("Tuna and Egg Salad", ["Lunch", "Dinner"], "Mediterranean", 12, [
        ("Tuna (Canned in Water)", 100, True), ("Egg (Whole)", 50, True),
        ("Lettuce (Romaine)", 80, True), ("Tomato", 100, True), ("Lemon", 15, True),
        ("Extra Virgin Olive Oil", 5, True)]),
    ("Cottage Cheese and Vegetable Plate", ["Dinner"], "Everyday", 5, [
        ("Cottage Cheese (Low-Fat)", 200, True), ("Cucumber", 100, True), ("Tomato", 100, True),
        ("Bell Pepper (Red)", 60, True), ("Whole Wheat Bread", 30, True)]),
    ("Tilapia with Salad", ["Dinner"], "Levantine", 20, [
        ("Tilapia", 150, True), ("Lettuce (Romaine)", 80, True), ("Tomato", 100, True),
        ("Cucumber", 80, True), ("Lemon", 15, True), ("Extra Virgin Olive Oil", 5, True)]),
    # --- snacks -------------------------------------------------------------
    ("Dates and Walnuts", ["Snack"], "Levantine", 1, [
        ("Dates (Medjool)", 40, True), ("Walnuts", 20, True)]),
    ("Labneh and Cucumber", ["Snack"], "Levantine", 3, [
        ("Labneh", 100, True), ("Cucumber", 100, True), ("Extra Virgin Olive Oil", 5, True)]),
    ("Hummus with Pita", ["Snack"], "Levantine", 3, [
        ("Hummus (Prepared)", 100, True), ("Pita Bread (Whole Wheat)", 50, True), ("Cucumber", 60, True)]),
    ("Ayran and Almonds", ["Snack"], "Levantine", 1, [
        ("Ayran", 250, True), ("Almonds", 25, True)]),
    ("Fruit and Pistachios", ["Snack"], "Levantine", 2, [
        ("Apple", 120, True), ("Pistachios", 20, True)]),
    ("Halawa with Bread", ["Snack"], "Levantine", 2, [
        ("Halawa (Tahini Sweet)", 30, True), ("Pita Bread (White)", 50, True)]),
    ("Cheese, Olives and Tomato", ["Snack"], "Levantine", 3, [
        ("Nabulsi Cheese", 50, True), ("Green Olives", 25, True), ("Tomato", 100, True)]),
    ("Pomegranate and Yogurt", ["Snack"], "Levantine", 3, [
        ("Plain Yogurt", 150, True), ("Pomegranate", 100, True)]),
    ("Baba Ghanoush with Pita", ["Snack"], "Levantine", 3, [
        ("Baba Ghanoush", 100, True), ("Pita Bread (White)", 50, True)]),
    ("Makdous and Bread", ["Snack", "Breakfast"], "Levantine", 2, [
        ("Makdous", 60, True), ("Pita Bread (White)", 60, True), ("Cucumber", 60, True)]),
    ("Boiled Egg and Cucumber", ["Snack"], "Everyday", 8, [
        ("Egg (Whole)", 100, True), ("Cucumber", 100, True), ("Za'atar Mix", 3, True)]),
    ("Cottage Cheese and Fruit", ["Snack", "Breakfast"], "Everyday", 2, [
        ("Cottage Cheese (Low-Fat)", 150, True), ("Peach", 120, True)]),
    ("Carrots and Hummus", ["Snack"], "Everyday", 2, [
        ("Carrot", 120, True), ("Hummus (Prepared)", 80, True)]),
    ("Walnuts and Figs", ["Snack"], "Levantine", 1, [
        ("Walnuts", 20, True), ("Fig (Fresh)", 100, True)]),
]


#: Recipes whose name no longer describes what is in them. A dish called after an
#: ingredient it does not contain is the fastest way to lose a user's trust in the whole
#: feature, and two of these sixteen were: this one carried white rice and no freekeh,
#: because the catalogue holds none, and "Avocado Toast with Eggs" carried oats and no
#: bread while the catalogue held six breads. Renaming here rather than in the table
#: alone, so a database that already ran the seed converges too.
# A dish whose own name carries a parenthetical nests inside a substitution:
# "Lentils and Rice (Mujadara style) (Quinoa instead of White Rice)".
RENAMED = {"Chicken Freekeh Bowl": "Chicken and Rice with Spinach",
           "Lentils and Rice (Mujadara style)": "Mujadara Style Lentils and Rice",
           "Eggplant Moussaka (Levantine)": "Levantine Eggplant Moussaka"}


class Command(BaseCommand):
    help = "Seed a starter recipe library mapped onto the existing food catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--replace", action="store_true",
                            help="delete existing seeded recipes first")

    @transaction.atomic
    def handle(self, *args, **opts):
        from diet.models import FoodItem, Recipe, RecipeIngredient

        # Every lookup in this command is on `name_en`, the unique column and the one
        # that means "this recipe". `name` is a modeltranslation virtual field and
        # resolves against the ACTIVE language.
        if opts["replace"]:
            deleted = Recipe.objects.filter(name_en__in=[r[0] for r in RECIPES]).delete()[0]
            self.stdout.write(f"removed {deleted} existing rows")

        created = updated = skipped = 0
        for was, now in RENAMED.items():
            # Rename only when the target name is free. This was a subquery on
            # `.values("name")`, which modeltranslation now expands to three columns
            # and Django refuses as a filter value.
            if not Recipe.objects.filter(name_en=now).exists():
                Recipe.objects.filter(name_en=was).update(name=now, name_en=now)
        Recipe.objects.filter(name_en__in=RENAMED).delete()
        for name, meals, cuisine, minutes, lines in RECIPES:
            foods = {}
            missing = []
            for food_name, grams, scalable in lines:
                # Exact first. Failing that, the *shortest* containing name, because
                # `.first()` on an unordered icontains picks arbitrarily: asking for
                # "Olive Oil" against a catalogue holding both "Extra Virgin Olive Oil"
                # and "Olive Oil Spray" would take whichever the database returned
                # first, and the macros of the two are not the same food.
                # `name_en`, not `name`. `name` is a modeltranslation virtual field, so
                # every lookup here resolved against the ACTIVE language: run under any
                # non-English locale this matched nothing, printed 105 lines of
                # "skip ...: catalogue has no [...]" and exited 0, seeding no recipes at
                # all — and `load_food_catalogue` calls this command internally, so the
                # engine's whole culinary knowledge would silently be absent. Every
                # sibling was hardened for this; this file was missed.
                food = FoodItem.objects.filter(name_en__iexact=food_name).first()
                if food is None:
                    candidates = sorted(
                        FoodItem.objects.filter(name_en__icontains=food_name),
                        key=lambda f: (len(f.name_en or ""), f.name_en or ""),
                    )
                    food = candidates[0] if candidates else None
                    if food is not None:
                        self.stdout.write(
                            f"  {name!r}: {food_name!r} -> {food.name_en!r}")
                if food is None:
                    missing.append(food_name)
                else:
                    foods[food_name] = food
            if missing:
                skipped += 1
                self.stdout.write(f"  skip {name!r}: catalogue has no {missing}")
                continue

            # Reconcile, do not skip. `get_or_create` plus `continue` meant this file
            # stopped being the source of truth the moment a database had run it once:
            # correcting a recipe's ingredients, its meal types or its scalable flags
            # here changed nothing anywhere the seed had already been applied. That is
            # how twelve recipes kept a pinned fat line long after it was known to be
            # the reason no dish could reach its fat target.
            # Keyed on `name_en`, which is the unique column and the one that means
            # "this recipe". `get_or_create(name=...)` reads the active language and
            # would create 105 rows with `name_en` NULL under any other locale.
            recipe = Recipe.objects.filter(name_en=name).first()
            was_new = recipe is None
            if was_new:
                recipe = Recipe.objects.create(
                    name=name, name_en=name, meal_types=meals, cuisine=cuisine,
                    prep_minutes=minutes,
                    description=f"{cuisine} · about {minutes} minutes")
            if not was_new:
                recipe.meal_types = meals
                recipe.cuisine = cuisine
                recipe.prep_minutes = minutes
                recipe.save(update_fields=["meal_types", "cuisine", "prep_minutes"])
                updated += 1
            else:
                created += 1
            name_ar = NAMES_AR.get(name)
            if name_ar and getattr(recipe, "name_ar", None) != name_ar:
                recipe.name_ar = name_ar
                recipe.save(update_fields=["name_ar"])

            wanted = {foods[food_name].id: (grams, scalable)
                      for food_name, grams, scalable in lines}
            recipe.ingredients.exclude(food_id__in=wanted).delete()
            for line in recipe.ingredients.all():
                grams, scalable = wanted.pop(line.food_id)
                if float(line.grams or 0) != float(grams) or bool(line.scalable) != scalable:
                    line.grams, line.scalable = grams, scalable
                    line.save(update_fields=["grams", "scalable"])
            for food_id, (grams, scalable) in wanted.items():
                RecipeIngredient.objects.create(
                    recipe=recipe, food_id=food_id, grams=grams, scalable=scalable)

        self.stdout.write(self.style.SUCCESS(
            f"seeded {created} recipe(s); reconciled {updated}"))
