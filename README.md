# JBIG2 PDF optimiser
Recompresses 1-bit images in PDFs with global dictionary JBIG2 images

## Why

`ocrmypdf` and Adobe Acrobat use JBIG2 encoding for scans since it is more efficient that older CCITT Group 4. JBIG2 works like a set of stamps: instead of repeatedly providing a bitmap for every `e`, it creates a few stamps of `e` and then uses an `e` from that set of stamps whenever rendering an `e` is needed.

However, `ocrmypdf` and Acrobat usually only makes this set of stamps (a symbol dictionary) at the page level. JBIG2 also supports a global mode which can save even more space by combining symbol dictionaries across pages. The optimiser extracts 1-bit images across pages in a PDF. It then re-encodes them in JBIG2 in chunks (default 128 images) that share a global dictionary. It then replaces the original 1-bit images in the PDF with the new global dictionary JBIG2 images. This can reduce storage usage substantially, provided that encoding similarity thresholds are chosen carefully.

## Command

```
jb2_pdf_optimiser.py INPUT OUTPUT
```

See further details with `-h`. The default JBIG2 threshold is `0.8`.

## Comparison

Try this with your own PDFs, but these are some test cases on book scans. The test cases are denominated in terms of the number of images and their format, here mostly pages.

| Test case              | Original | Adobe Acrobat  | JBIG2 PDF Optimizer |
| :--------------------- | :------- | :------------- | :------------------ |
| 1 JPG + 141 CCITT G4   | 9.23 MB  | 1.77 MB (-81%) | 1.46 MB (-85%)      |
| 1018 local JBIG2 et al | 19.5 MB  | Error          | 13.0 MB (-33%)      |
| 1 JPG + 484 CCITT G4   | 19.1 MB  | 7.59 MB (-61%) | 4.51 MB (-76%)      |

The 1080 local JBIG2 PDF emitted an image processing error when compression was attempted in Adobe Acrobat (versions 11 and DC 2025).
