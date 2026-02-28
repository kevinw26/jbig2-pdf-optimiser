# JBIG2 PDF optimiser
Recompresses 1-bit images in PDFs with global dictionary JBIG2 images.

## Why

`ocrmypdf` and Adobe Acrobat use JBIG2 encoding for scans since it is more efficient that older CCITT Group 4. JBIG2 works like a set of stamps: instead of repeatedly providing a bitmap for every `e`, it creates a few stamps of `e` and then uses an `e` from that set of stamps whenever rendering an `e` is needed.

However, `ocrmypdf` and Acrobat usually only makes this set of stamps (a symbol dictionary) at the page level. JBIG2 also supports a global mode which can save even more space by combining symbol dictionaries across pages. The optimiser extracts 1-bit images across pages in a PDF. It then re-encodes them in JBIG2 in chunks (default 128 images) that share a global dictionary. It then replaces the original 1-bit images in the PDF with the new global dictionary JBIG2 images. This can reduce storage usage substantially, provided that encoding similarity thresholds are chosen carefully.

## Command

```
jb2_pdf_optimiser.py INPUT OUTPUT
```

See further details with `-h`. The default JBIG2 threshold is `0.8`.
