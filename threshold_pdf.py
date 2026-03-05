import argparse
import gc
import multiprocessing
import multiprocessing as mp
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from os import path
from typing import List

import imageio
import numpy as np
import pandas as pd
import pikepdf
from PIL import Image
from PIL.Image import Dither
from pikepdf import Name, StreamDecodeLevel, ObjectStreamMode
from skimage.filters import threshold_sauvola
from skimage.util import img_as_ubyte
from tqdm import tqdm

__version__ = '0.2.3'

WORKERS = max(multiprocessing.cpu_count() - 1, 1)
FILTER_NAMES = {
    '/DCTDecode'      : 'JPEG',
    '/JPXDecode'      : 'JPEG2000',
    '/JBIG2Decode'    : 'JBIG2',
    '/FlateDecode'    : 'FlateDecode (PNG/zlib)',
    '/LZWDecode'      : 'LZW',
    '/CCITTFaxDecode' : 'CCITT Fax',
    '/RunLengthDecode': 'RLE',
}


def save_image(image_mode, image_size, some_bytes, output_path):
    Image.frombytes(image_mode, image_size, some_bytes).save(output_path)


def is_identity_decode(obj) -> bool:
    decode = obj.get("/Decode")
    if decode is None:
        return True
    pairs = [float(v) for v in decode]
    return pairs == {
        1: [0, 1],
        3: [0, 1, 0, 1, 0, 1],
        4: [0, 1, 0, 1, 0, 1, 0, 1],
    }[len(pairs) // 2]


def get_image_info(xobj):
    csp = xobj.get('/ColorSpace')
    bpc = int(xobj.get('/BitsPerComponent', 8))

    cs_name = (
        str(csp[0]) if isinstance(csp, pikepdf.Array) else
        str(csp) if csp is not None else
        '/DeviceRGB'
    )
    if bpc == 1:
        color_type = '1-bit'
    elif cs_name in ('/DeviceGray', '/CalGray'):
        color_type = 'greyscale'
    elif cs_name in ('/DeviceRGB', '/CalRGB', '/DeviceCMYK'):
        color_type = cs_name.lstrip('/').lower()  # devicergb or devicecmyk'
    elif cs_name == '/ICCBased':
        # Check number of components from the ICC stream
        icc_stream = xobj['/ColorSpace'][1]
        n = int(icc_stream.get('/N', 3))
        color_type = 'greyscale' if n == 1 else 'colour'
    else:
        color_type = f'unknown ({cs_name})'

    filter_type = xobj.Filter
    if filter_type is None:
        encoding = 'uncompressed'
    elif isinstance(filter_type, pikepdf.Array):
        encoding = ' + '.join(FILTER_NAMES.get(str(f), str(f)) for f in filter_type)
    else:
        encoding = FILTER_NAMES.get(str(filter_type), str(filter_type))

    return color_type, encoding


def convert_to_rbg(img: Image.Image) -> np.ndarray:
    if img.mode not in ('L', '1'):
        img = img.convert('RGB')
    return np.array(img)


class BoundedProcessPoolExecutor(ProcessPoolExecutor):
    # https://stackoverflow.com/a/78071937
    def __init__(self, max_queue=WORKERS * 2, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.semaphore = multiprocessing.Semaphore(max_queue)

    def submit(self, *args, **kwargs):
        self.semaphore.acquire()
        future = super().submit(*args, **kwargs)
        future.add_done_callback(lambda _: self.semaphore.release())
        return future


def extract_all_images(
        pdf_object: pikepdf.Pdf, extract_to=None, print_catalogue=False,
        skip: List = None) -> pd.DataFrame:
    if skip is None: skip = []

    img_id = 0
    rows = []
    with BoundedProcessPoolExecutor(max_workers=WORKERS) as ex:
        ff = []
        for page_num, page in enumerate(tqdm(pdf_object.pages, desc='reading images from pages')):
            objs = page.get("/Resources", {}).get("/XObject", {})
            for name, obj in objs.items():
                try:
                    if isinstance(obj,
                                  pikepdf.Stream) and obj.Subtype == Name.Image and is_identity_decode(
                        obj):
                        # inspired by ocrmypdf: don't mess with non-identity decodes
                        if img_id in skip:
                            img_id += 1  # consistent indexing
                            continue

                        cs, enc = get_image_info(obj)
                        d = {
                            'page'     : page_num,
                            'index'    : img_id,
                            'pointer'  : obj,
                            'orig_size': len(obj.read_raw_bytes()),
                            'colour'   : cs,
                            'encoding' : enc
                        }
                        if extract_to is not None:
                            output_path = path.join(extract_to, f'img_{img_id:06d}.png')
                            pil_image = pikepdf.PdfImage(obj).as_pil_image()
                            if WORKERS > 1:
                                ff.append(ex.submit(
                                    save_image, pil_image.mode, pil_image.size, pil_image.tobytes(),
                                    output_path))
                            else:
                                # if single threaded ignore the executor
                                save_image(pil_image.mode, pil_image.size, pil_image.tobytes(),
                                           output_path)
                            d['output_path'] = output_path
                        rows.append(d)
                        img_id += 1
                except (AttributeError, KeyError, pikepdf.PdfError):
                    pass
        catalogue_df = pd.DataFrame(rows)

        # check results; progress bar unnecessary
        if len(ff) > 0:
            if extract_to is None:
                raise ValueError('image extraction not requested but extraction jobs queued???')
            tqdm.write('waiting for intermediate files to finish writing', end=' ... ')
            try:
                for f in as_completed(ff):
                    f.result()
            except Exception as e:
                ex.shutdown(wait=False, cancel_futures=True)
                raise e
            tqdm.write('done')

    if print_catalogue:
        more_drop = ['output_path'] if extract_to is None else []
        tqdm.write(
            catalogue_df.drop(columns=['pointer'] + more_drop, errors='ignore')
            .to_string(index=False))

    return catalogue_df


def local_threshold_image(img, threshold=None):
    with tempfile.NamedTemporaryFile(suffix='.tif') as temp_file:
        pixel_array = convert_to_rbg(Image.open(img))
        imageio.imwrite(temp_file.name, img_as_ubyte(pixel_array > threshold_sauvola(pixel_array)),
                        compression=8)
        del pixel_array
        jb2_call = subprocess.run(['jbig2', '-p', temp_file.name], capture_output=True, check=True)
        return jb2_call.stdout


def dither_image(img, threshold=None):
    with tempfile.NamedTemporaryFile(suffix='.tif') as temp_file:
        the_image = Image.open(img)
        the_image.convert('1', dither=Dither.FLOYDSTEINBERG).save(
            temp_file.name, compression='group4')
        del the_image
        jb2_call = subprocess.run(['jbig2', '-p', temp_file.name], capture_output=True, check=True)
        return jb2_call.stdout


def global_threshold_image(img, threshold=0.5):
    with tempfile.NamedTemporaryFile(suffix='.tif') as temp_file:
        pixel_array = convert_to_rbg(Image.open(img))
        imageio.imwrite(temp_file.name, img_as_ubyte(pixel_array > threshold), compression=8)
        del pixel_array
        jb2_call = subprocess.run(['jbig2', '-p', temp_file.name], capture_output=True, check=True)
        return jb2_call.stdout


def save_pdf(the_pdf, o_path):
    the_pdf.remove_unreferenced_resources()
    the_pdf.save(o_path, compress_streams=True, recompress_flate=True, linearize=True,
                 stream_decode_level=StreamDecodeLevel.generalized,
                 object_stream_mode=ObjectStreamMode.generate)


class PDFThresholder:

    @staticmethod
    def _substitute_jb2image(pointer, image_bytes):
        for key in [
            '/DecodeParms',  # generic
            '/BlackIs1',  # ccitt
            '/ColorTransform',  # jpeg
            '/SMask', '/Decode', '/Alternates', '/Intent'  # colour
        ]:
            if key in pointer:
                del pointer[key]

        pointer.ColorSpace = Name.DeviceGray
        pointer.BitsPerComponent = 1
        pointer.write(image_bytes, filter=Name.JBIG2Decode)

    def __init__(self, input_pdf, output_pdf, skip: List = None, method='sauvola', threshold=None):
        if skip is None:
            skip = []
        self.input_pdf = input_pdf
        self.output_pdf = output_pdf
        self.skip = skip
        self.method = method
        self.threshold = threshold
        self.pdf = pikepdf.Pdf.open(self.input_pdf)

    def execute(self):
        if self.method == 'global' and self.threshold is None:
            raise ValueError(f'global thresholding requested but threshold missing')

        with tempfile.TemporaryDirectory() as temp_dir:
            catalogue_df = extract_all_images(self.pdf, extract_to=temp_dir, skip=self.skip)
            gc.collect()  # clean up memory if possible

            method_to_use = (
                local_threshold_image if self.method == 'sauvola' else
                dither_image if self.method == 'dither' else
                global_threshold_image if self.method == 'threshold' else None
            )
            if method_to_use is None:
                raise NotImplementedError(f'method {self.method} not implemented')

            new_bytes = []
            pbar = tqdm(total=len(catalogue_df), desc='thresholding images')
            if WORKERS > 1:
                with ProcessPoolExecutor(max_workers=WORKERS) as ex:
                    ff = [ex.submit(method_to_use, i, threshold=self.threshold)
                          for i in catalogue_df['output_path'].values]
                    try:
                        for _ in as_completed(ff):
                            pbar.update()
                    except Exception as e:
                        ex.shutdown(wait=False, cancel_futures=True)
                        raise e
                    new_bytes = [f.result() for f in ff]  # in order
            else:
                new_bytes.extend(
                    method_to_use(i, threshold=self.threshold)
                    for i in catalogue_df['output_path'].values)

            catalogue_df['threshold_bytes'] = new_bytes
            for _, r in catalogue_df.iterrows():
                PDFThresholder._substitute_jb2image(r['pointer'], r['threshold_bytes'])

        save_pdf(self.pdf, self.output_pdf)
        self.pdf.close()


if __name__ == '__main__':

    mp.set_start_method('spawn')
    psr = argparse.ArgumentParser(
        description='Turn images in a PDF file into 1-bit images. By default uses local adaptive '
                    'thresholding.')
    psr.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}')
    psr.add_argument('input_pdf')
    psr.add_argument('output_pdf', default=None, nargs='?')
    psr.add_argument(
        '--global-threshold', type=float, default=None,
        help='Use a global brightness threshold')
    psr.add_argument(
        '--dither', action='store_true', help='Use Floyd-Steinberg dithering')
    psr.add_argument(
        '--workers', type=int, default=WORKERS,
        help=f'Number of worker processes (default: {WORKERS})')
    psr.add_argument(
        '--catalogue-only', action='store_true',
        help='Print a list of all images in the PDF with format, size, and colour space, then exit')
    psr.add_argument(
        '--skip',
        type=int, nargs='+', metavar='N',
        help='List of images to skip by catalogue index (eg --skip 0 1 2)')

    args = psr.parse_args()
    if args.output_pdf is None and not args.catalogue_only:
        psr.error(f'No output PDF location provided')
    if not path.exists(args.input_pdf):
        psr.error(f'Input PDF does not exist')
    if args.global_threshold is not None and not (0 <= args.global_threshold <= 1):
        psr.error(f'Global threshold {args.global_threshold} is not between 0 and 1')
    if shutil.which('jbig2') is None:
        psr.error(
            'JBIG2 encoder not found. See https://ocrmypdf.readthedocs.io/en/latest/jbig2.html')

    # set the global workers
    WORKERS = args.workers if args.workers != WORKERS else WORKERS

    if args.catalogue_only:
        with pikepdf.open(args.input_pdf) as pdf:
            extract_all_images(pdf, extract_to=None, print_catalogue=True)
        sys.exit(0)

    if args.global_threshold is not None:
        PDFThresholder(
            args.input_pdf, args.output_pdf, skip=args.skip, method='threshold',
            threshold=args.global_threshold
        ).execute()
        sys.exit(0)

    if args.dither:
        PDFThresholder(args.input_pdf, args.output_pdf, skip=args.skip, method='dither').execute()
        sys.exit(0)

    PDFThresholder(args.input_pdf, args.output_pdf, skip=args.skip, method='sauvola').execute()
