# SPDX-FileCopyrightText: 2026 Kevin Wong
# SPDX-License-Identifier: GPL-3

import argparse
import os
import shutil
import warnings
from os import path
from subprocess import Popen, STDOUT, PIPE
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import pikepdf
from pikepdf import ObjectStreamMode, Name
from pikepdf import StreamDecodeLevel
from tqdm import tqdm

__version__ = '0.1.2'


def save_pdf(pdf, o_path):
    pdf.remove_unreferenced_resources()
    pdf.save(o_path, compress_streams=True, recompress_flate=True,
             linearize=True, stream_decode_level=StreamDecodeLevel.generalized,
             object_stream_mode=ObjectStreamMode.generate)


class JBIG2PDFOptimiser:

    @staticmethod
    def _calc_file_diffs(input_pdf, output_pdf):
        # check actual file sizes on disk
        diffs = pd.DataFrame([{
            'orig_kb': path.getsize(input_pdf),
            'opt_kb' : path.getsize(output_pdf),
        }])
        diffs['diff_kb'] = diffs['orig_kb'] - diffs['opt_kb']
        diffs['diff_pc'] = diffs['diff_kb'] / diffs['orig_kb']
        for c in [c for c in diffs.columns if c.endswith('_kb')]:
            diffs[c] = diffs[c].round(1).apply(lambda d: f'{d / 1024:.1f} kb')
        for c in [c for c in diffs.columns if c.endswith('_pc')]:
            diffs[c] = diffs[c].round(1).apply(lambda d: f'{100 * d:.2f}%')
        return diffs

    def __init__(self, input_pdf: str, output_pdf: str, chunk_size: int = 128,
                 jb2_threshold: float = 0.8):
        self.input_pdf = input_pdf
        self.output_pdf = output_pdf
        self.chunk_size = chunk_size
        self.jb2_threshold = jb2_threshold

        self.pdf = pikepdf.Pdf.open(input_pdf)
        self.df = pd.DataFrame()

    def _substitute_jb2global(self, target_obj, new_data, globals_obj):
        keys_to_remove = ['/CCITTFaxDecode', '/BlackIs1']
        for key in keys_to_remove:
            if key in target_obj:
                del target_obj[key]

        jbig2_params = self.pdf.make_indirect({'/JBIG2Globals': globals_obj})
        target_obj.write(new_data, filter=pikepdf.Name.JBIG2Decode, decode_parms=jbig2_params)

    def extract_images(self, tmp_dir: str):
        rows = []
        for obj_num in tqdm(range(1, len(self.pdf.objects)), desc='extracting images from objects'):
            try:
                obj = self.pdf.get_object(obj_num, 0)
                if isinstance(obj, pikepdf.Stream) and obj.Subtype == Name.Image \
                        and obj.BitsPerComponent == 1:
                    if Name.Decode in obj:
                        # like ocrmypdf don't mess with custom decodes
                        continue

                    img_id = len(rows)
                    pbm_path = path.join(tmp_dir, f'img_{img_id:06d}.pbm')
                    pikepdf.PdfImage(obj).as_pil_image().save(pbm_path)
                    rows.append({
                        'obj_ptr'  : obj,
                        'pbm_path' : pbm_path,
                        'orig_size': len(obj.read_raw_bytes())
                    })
            except (AttributeError, KeyError, pikepdf.PdfError):
                continue
        self.df = pd.DataFrame(rows)

    def compress_and_replace(self, tmp_dir: str):
        """By chunk create dictionary and re-encode images as JBIG2"""
        chunks = np.array_split(self.df.index, np.ceil(len(self.df) / self.chunk_size))
        pbar = tqdm(
            desc=f'encoding jbig2 images in {len(chunks)} chunks of ca {len(chunks[0])} images',
            total=len(self.df))
        for chunk_id, chunk_idx in enumerate(chunks):
            chunk_df = self.df.loc[chunk_idx]
            chunk_dir = path.join(tmp_dir, f'chunk_{chunk_id}')
            os.makedirs(chunk_dir)

            pbm_files = chunk_df['pbm_path'].to_list()
            with Popen(
                    ['jbig2', '-s', '-p', '-t', f'{self.jb2_threshold:.2f}', '-v', *pbm_files],
                    stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, cwd=chunk_dir
            ) as proc:
                for line in proc.stdout:
                    if line.startswith('thresholded'):
                        # update progress bar as images are encoded; replacement is fast
                        pbar.update()

            sym_file = path.join(chunk_dir, 'output.sym')
            with open(sym_file, 'rb') as f:
                symbol_data = f.read()
                jb2_globals = self.pdf.make_stream(symbol_data)

            for i, (idx, row) in enumerate(chunk_df.iterrows()):
                fragment_file = path.join(chunk_dir, f'output.{i:04d}')
                with open(fragment_file, 'rb') as f:
                    compressed_data = f.read()
                    self.df.loc[idx, 'chunk'] = chunk_id
                    self.df.loc[idx, 'jb2_lsize'] = len(compressed_data)
                    self.df.loc[idx, 'jb2_gsize'] = len(symbol_data)
                    self._substitute_jb2global(row['obj_ptr'], compressed_data, jb2_globals)

    def optimize(self, save_csv=None):
        with TemporaryDirectory() as tmp_dir:
            self.extract_images(tmp_dir)
            if self.df.empty:
                print('no 1-bit optimisable images found')
                return

            self.compress_and_replace(tmp_dir)
            save_pdf(self.pdf, self.output_pdf)

        tqdm.write('pre and post file sizes')
        diffs = JBIG2PDFOptimiser._calc_file_diffs(self.input_pdf, self.output_pdf)
        tqdm.write(diffs.to_string(index=False))

        if save_csv is not None:
            tqdm.write(f'\nsaving diagnostic csv to {save_csv}')

            # clean up chunk from float to int64
            df = self.df.drop(columns=['obj_ptr'])
            df['chunk'] = df['chunk'].astype('Int64')
            df = df.set_index(['chunk', 'pbm_path'])

            # calc estimated savings
            df['jb2_est_size'] = df['jb2_lsize'] + (
                    df.groupby('chunk')['jb2_gsize'].mean() / self.df.groupby('chunk').size())
            df['savings_pc'] = (1 - (df['jb2_est_size'] / df['orig_size'])).mul(100).round(2)
            df.to_csv(save_csv)


if __name__ == '__main__':

    # parse arguments
    psr = argparse.ArgumentParser(
        prog='jb2_pdf_optimiser.py',
        description='Recompress 1-bit images in PDFs with global dictionary JBIG2 images')
    psr.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}')
    psr.add_argument('input_pdf')
    psr.add_argument('output_pdf')
    psr.add_argument(
        '-t', '--threshold',
        type=float, default=0.82,
        # based on testing with real world scans, thresholds below 0.8 have high chances of
        # substituting e -> c, especially with smaller font sizes (eg footnotes); at 0.75 these
        # issues are prevalent. at 0.7 the symbol substitution errors intrude into the main text
        #
        # the thresholds have diminishing returns to compression:
        #
        # | threshold | kb  | incremental | diff      |
        # |-----------|-----|-------------|-----------|
        # | 0.7       | 195 | 2.99%       | 15.95%    |
        # | 0.75      | 201 | 5.63%       | 13.36%    |
        # | 0.8       | 213 | 8.19%       | 8.19%     |
        # | 0.85      | 232 | 10.42%      | 0.00%     |
        # | 0.9       | 259 | 36.21%      | -11.64%   |
        # | 1         | 406 |             | -75.00%   |
        #
        # based on testing 0.8 is the smallest threshold without symbol substitution issues in the
        # pdfs tested. 0.02 is added to be on the safe side
        help='JBIG2 similarity threshold')
    psr.add_argument(
        '-c', '--chunk',
        type=int, default=128, help='Number of images per JBIG2 global dictionary')
    psr.add_argument(
        '--diag-csv', type=str, default=None, help='Path to output CSV with image data')
    args = psr.parse_args()

    # validate inputs
    if not (0.4 <= args.threshold <= 0.97):
        psr.error(f'JBIG2 similarity threshold must be between 0.4 and 0.97')
    if shutil.which('jbig2') is None:
        psr.error(
            'Jbig2 executable not found. See https://ocrmypdf.readthedocs.io/en/latest/jbig2.html')
    if not path.isfile(args.input_pdf):
        psr.error(f'Input PDF does not exist')

    # emit warnings if the JBIG2 similarity threshold is below 0.8
    if args.threshold <= 0.8:
        warnings.warn('JBIG2 thresholds below 0.8 have high changes of symbol substitution')

    JBIG2PDFOptimiser(
        input_pdf=args.input_pdf, output_pdf=args.output_pdf,
        chunk_size=args.chunk, jb2_threshold=args.threshold
    ).optimize(save_csv=args.diag_csv)
