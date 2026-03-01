# SPDX-FileCopyrightText: 2026 Kevin Wong
# SPDX-License-Identifier: GPL-3
import re
import subprocess
from difflib import context_diff, unified_diff
from glob import glob
from os import path

import numpy as np
import pikepdf

from jb2_pdf_optimiser import JBIG2PDFOptimiser


def extract_test_pages(dir, test_data):
    pdfs = glob(path.join(dir, '*.pdf'))

    seen_prefixes = set()
    new_pdf = pikepdf.Pdf.new()
    for p in [
        i for i in pdfs if
        re.match(r'_unpaper(_extract)?\.pdf$', path.basename(i)) or
        not path.basename(i).endswith('opt.pdf')
    ]:
        prefix = path.basename(p).split('_')[0]
        if prefix in seen_prefixes: continue
        with pikepdf.open(p) as pdf:
            seen_prefixes.update(prefix)
            to_extract = np.astype(
                np.round(np.linspace(start=0, stop=len(pdf.pages), endpoint=False, num=4)),
                np.int64)[1:-1]
            [new_pdf.pages.append(list(pdf.pages)[i]) for i in to_extract]

    new_pdf.save(test_data)


if __name__ == '__main__':

    test_data = path.join('test_data', '_test_pages_unchanged.pdf')
    if not path.exists(test_data):
        extract_test_pages('test_data', test_data)
    subprocess.run(
        ['ocrmypdf', '--sidecar', 'test_data/_test_data_unchanged_ocr.txt', '--lang', 'eng+fra+lat',
         '--force-ocr', test_data, '/dev/null'])
    with open('test_data/_test_data_unchanged_ocr.txt', 'r') as f:
        truth = f.read()

    for threshold in sorted([0.70, 0.75, 0.80, 0.85, 0.90], reverse=True):
        test_output = test_data.replace('unchanged.pdf', f'threshold_{threshold:.2f}.pdf')
        text_output = path.join('test_data', '_test_data_unchanged.txt') \
            .replace('unchanged.txt', f'threshold_{threshold:.2f}.txt')
        JBIG2PDFOptimiser(test_data, test_output, jb2_threshold=threshold).optimize()
        subprocess.run(
            ['ocrmypdf', '--sidecar', text_output, '--lang', 'eng+fra+lat', '--force-ocr',
             test_output, '/dev/null'])

        with open(text_output, 'r') as f:
            the_text = f.read()
        with open(re.sub(r'\.txt', f'_diff.txt', text_output), 'w') as f:
            c_diff = '\n'.join(list(unified_diff(truth, the_text)))
            print(c_diff)
            f.write(c_diff)

    new_pdf = pikepdf.Pdf.new()
    with pikepdf.Pdf.open(test_data) as og_pdf:
        total_pages = len(og_pdf.pages)
        for i in range(total_pages):
            for f in sorted(glob('test_data/jbig2_char_sub_test/*.pdf')):
                with pikepdf.open(f) as pdf:
                    new_pdf.pages.append(list(pdf.pages)[i])

    new_pdf.save('test_data/jbig2_char_sub_test/_test_pages_collated.pdf')
