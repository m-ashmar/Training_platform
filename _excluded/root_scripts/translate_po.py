import polib
import time
from deep_translator import GoogleTranslator

def translate_po_file(filepath):
    po = polib.pofile(filepath)
    translator = GoogleTranslator(source='en', target='ar')
    
    count = 0
    for entry in po:
        if not entry.msgstr or 'fuzzy' in entry.flags:
            try:
                translated = translator.translate(entry.msgid)
                entry.msgstr = translated
                if 'fuzzy' in entry.flags:
                    entry.flags.remove('fuzzy')
                count += 1
                if count % 20 == 0:
                    print(f"Translated {count} strings...")
                time.sleep(0.1) # Be nice to the API
            except Exception as e:
                print(f"Error translating '{entry.msgid}': {e}")
                
    po.save(filepath)
    print(f"Successfully translated {count} missing/fuzzy strings.")

if __name__ == '__main__':
    translate_po_file('/Users/mac/Documents/Training_platform/locale/ar/LC_MESSAGES/django.po')
