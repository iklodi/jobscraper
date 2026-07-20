import os
from docx2pdf import convert
from docx import Document

test_dir = os.path.expanduser('~/Documents/JobScraper_Temp')
os.makedirs(test_dir, exist_ok=True)

test_docx = os.path.join(test_dir, 'test_conv.docx')
doc = Document()
doc.add_paragraph('Hello world')
doc.save(test_docx)

try:
    convert(test_docx)
    print("Conversion successful in ~/Documents!")
except Exception as e:
    print(f"Failed: {e}")
