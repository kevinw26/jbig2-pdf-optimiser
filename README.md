# JBIG2 PDF optimiser
Recompresses 1-bit images in PDFs with global dictionary JBIG2 images.

## Why

`ocrmypdf` and Adobe Acrobat use JBIG2 encoding for scans since it is more efficient that older CCITT Group 4. JBIG2 works like a set of stamps: instead of repeatedly providing a bitmap for every `e`, it creates a few stamps of `e` and then places its coordinates whenever `e` appears.

However, `ocrmypdf` and Acrobat usually only does this at the page level. JBIG2 also supports a global mode which can save even more space by combining symbol dictionaries across pages. This script extracts 1-bit images across pages in a PDF and re-encodes them in JBIG2 in chunks (default 128 images) that share a global dictionary. This can reduce space usage substantially, provided that encoding similarity thresholds are chosen carefully.

## Command

```
jb2_pdf_optimiser.py INPUT OUTPUT
```

See further details with `-h`.
