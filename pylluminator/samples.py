"""Class that holds a collection of Sample objects and defines methods for batch processing."""

import os
from inspect import signature

from importlib.resources.readers import MultiplexedPath

import pandas as pd

from pylluminator.sample import Sample
import pylluminator.sample_sheet as sample_sheet
from pylluminator.read_idat import IdatDataset
from pylluminator.annotations import Annotations, Channel
from pylluminator.utils import save_object, load_object, get_files_matching, mask_dataframe, get_logger, convert_to_path

LOGGER = get_logger()


class Samples:
    """Samples contain a collection of Sample objects in a dictionary, with sample names as keys.

    It also holds the sample sheet information and the annotation object. It is mostly used to apply functions to
    several samples at a time

    :ivar annotation: probes metadata. Default: None.
    :vartype annotation: Annotations | None
    :ivar sample_sheet: samples information given by the csv sample sheet. Default: None
    :vartype sample_sheet: pandas.DataFrame | None
    :ivar samples: the dictionary containing the samples. Default: {}
    :vartype samples: dict

    The following methods defined in Sample can be directly used on Samples object:
        - :func:`pylluminator.sample.Sample.apply_non_unique_mask`
        - :func:`pylluminator.sample.Sample.apply_quality_mask`
        - :func:`pylluminator.sample.Sample.apply_xy_mask`
        - :func:`pylluminator.sample.Sample.calculate_betas`
        - :func:`pylluminator.sample.Sample.dye_bias_correction`
        - :func:`pylluminator.sample.Sample.dye_bias_correction_nl`
        - :func:`pylluminator.sample.Sample.infer_type1_channel`
        - :func:`pylluminator.sample.Sample.merge_annotation_info`
        - :func:`pylluminator.sample.Sample.noob_background_correction`
        - :func:`pylluminator.sample.Sample.poobah`
        - :func:`pylluminator.sample.Sample.scrub_background_correction`
    """

    def __init__(self, sample_sheet_df: pd.DataFrame | None = None):
        """Initialize the object with only a sample-sheet.

        :param sample_sheet_df: sample sheet dataframe. Default: None
        :type sample_sheet_df: pandas.DataFrame | None"""
        self.annotation = None
        self.sample_sheet = sample_sheet_df
        self.samples = {}

    def __getitem__(self, item: int | str):
        if isinstance(item, int):
            return self.samples[list(self.samples.keys())[item]]
        return self.samples[item]

    def keys(self):
        """Return the names of the samples contained in this object"""
        return self.samples.keys()

    def __getattr__(self, method_name):
        """Wrapper for Sample methods that can directly be applied to every sample"""

        supported_functions = ['dye_bias_correction', 'dye_bias_correction_nl', 'noob_background_correction',
                               'scrub_background_correction', 'poobah', 'infer_type1_channel', 'apply_quality_mask',
                               'apply_non_unique_mask', 'apply_xy_mask', 'merge_annotation_info', 'calculate_betas']

        if callable(getattr(Sample, method_name)) and method_name in supported_functions:
            def method(*args, **kwargs):
                LOGGER.info(f'>> start {method_name}')
                [getattr(sample, method_name)(*args, **kwargs) for sample in self.samples.values()]

                # if the method called updated the beta values, update the dataframe
                if method_name == 'calculate_betas':
                    LOGGER.info(f'concatenating beta values dataframes')
                    self._betas_df = pd.concat([sample.betas(False) for sample in self.samples.values()], axis=1)
                elif method_name not in ['apply_quality_mask', 'apply_non_unique_mask', 'apply_xy_mask']:
                    self._betas_df = None

                LOGGER.info(f'done with {method_name}\n')

            method.__name__ = method_name
            method.__doc__ = getattr(Sample, method_name).__doc__
            method.__signature__ = signature(getattr(Sample, method_name))
            return method

        LOGGER.error(f'Undefined attribute/method {method_name} for class Samples')

    def betas(self, mask: bool = True) -> pd.DataFrame | None:
        """Return the beta values dataframe, and applies the current mask if the parameter mask is set to True (default).

       :param mask: True removes masked probes from betas, False keeps them. Default: True
       :type mask: bool
       :return: the beta values as a dataframe, or None if the beta values have not been calculated yet.
       :rtype: pandas.DataFrame | None"""
        if mask:
            masked_indexes = [sample.masked_indexes for sample in self.samples.values()]
            return mask_dataframe(self._betas_df, masked_indexes)
        else:
            return self._betas_df

    ####################################################################################################################
    # Properties
    ####################################################################################################################

    @property
    def nb_samples(self) -> int:
        """Count the number of samples contained in the object

        :return: number of samples
        :rtype: int"""
        return len(self.samples)

    ####################################################################################################################
    # Description, saving & loading
    ####################################################################################################################

    def __str__(self):
        return list(self.samples.keys())

    def __repr__(self):
        description = 'Samples object\n'
        description += '--------------\n'
        description += 'No sample' if self.samples is None else f'{self.nb_samples} samples: {self.__str__()}\n'
        description += 'No annotation\n' if self.annotation is None else self.annotation.__repr__()
        description += 'No sample sheet' if self.sample_sheet is None else (f'Sample sheet head: \n '
                                                                            f'{self.sample_sheet.head(3)}')
        return description

    def save(self, filepath: str) -> None:
        """Save the current Samples object to `filepath`, as a pickle file

        :param filepath: path to the file to create
        :type filepath: str

        :return: None"""
        save_object(self, filepath)

    @staticmethod
    def load(filepath: str):
        """Load a pickled Samples object from `filepath`

        :param filepath: path to the file to read
        :type filepath: str

        :return: the loaded object"""
        return load_object(filepath, Samples)


def read_samples(datadir: str | os.PathLike | MultiplexedPath,
                 sample_sheet_df: pd.DataFrame | None = None,
                 sample_sheet_name: str | None = None,
                 annotation: Annotations | None = None,
                 max_samples: int | None = None,
                 min_beads=1,
                 keep_idat=False) -> Samples | None:
    """Search for idat files in the datadir through all sublevels.

    The idat files are supposed to match the information from the sample sheet and follow this naming convention:
    `*[sentrix ID]*[sentrix position]*[channel].idat` where `*` can be any characters.
    `channel` must be spelled `Red` or `Grn`.

    :param datadir:  directory where sesame files are
    :type datadir: str  | os.PathLike | MultiplexedPath

    :param sample_sheet_df: samples information. If not given, will be automatically rendered. Default: None
    :type sample_sheet_df: pandas.DataFrame | None

    :param sample_sheet_name: name of the csv file containing the samples' information. You cannot provide both a sample
        sheet dataframe and name. Default: None
    :type sample_sheet_name: str | None

    :param annotation: probes information. Default None.
    :type annotation: Annotations | None

    :param max_samples: set it to only load N samples to speed up the process (useful for testing purposes). Default: None
    :type max_samples: int | None

    :param min_beads: filter probes that have less than N beads. Default: 1
    :type min_beads: int

    :param keep_idat: if set to True, keep idat data after merging the annotations. Default: False
    :type: bool

    :return: Samples object or None if an error was raised
    :rtype: Samples | None"""
    datadir = convert_to_path(datadir)  # expand user and make it a Path
    LOGGER.info(f'>> start reading sample files from {datadir}')

    if sample_sheet_df is not None and sample_sheet_name is not None:
        LOGGER.error('You can\'t provide both a sample sheet dataframe and name. Please only provide one parameter.')
        return None
    elif sample_sheet_df is None and sample_sheet_name is None:
        LOGGER.debug('No sample sheet provided, creating one')
        sample_sheet_df, _ = sample_sheet.create_from_idats(datadir)
    elif sample_sheet_name is not None:
        sample_sheet_df = sample_sheet.read_from_file(f'{datadir}/{sample_sheet_name}')

    # check that the sample sheet was correctly created / read. If not, abort mission.
    if sample_sheet_df is None:
        return None

    samples_dict = {}

    # for each sample
    for _, line in sample_sheet_df.iterrows():

        sample = Sample(line.sample_name)

        # read each channel file
        for channel in Channel:
            pattern = f'*{line.sample_id}*{channel}*.idat*'
            paths = [str(p) for p in get_files_matching(datadir, pattern)]
            if len(paths) == 0:
                if line.sentrix_id != '' and line.sentrix_position != '':
                    pattern = f'*{line.sentrix_id}*{line.sentrix_position}*{channel}*.idat*'
                    paths = [str(p) for p in get_files_matching(datadir, pattern)]
            if len(paths) == 0:
                LOGGER.error(f'no paths found matching {pattern}')
                continue
            if len(paths) > 1:
                LOGGER.error(f'Too many files found matching {pattern} : {paths}')
                continue

            LOGGER.debug(f'reading file {paths[0]}')
            # set the sample's idata for this channel
            sample.set_idata(channel, IdatDataset(paths[0]))

        if sample.idata is None:
            LOGGER.error(f'no idat files found for sample {line.sample_name}, skipping it')
            continue

        # add the sample to the dictionary
        samples_dict[line.sample_name] = sample

        # only load the N first samples
        if max_samples is not None and len(samples_dict) == max_samples:
            break

    samples = Samples(sample_sheet_df)
    samples.samples = samples_dict

    if annotation is not None:
        samples.annotation = annotation
        samples.merge_annotation_info(annotation, min_beads, keep_idat)

    LOGGER.info(f'reading sample files done\n')
    return samples


def from_sesame(datadir: str | os.PathLike | MultiplexedPath, annotation: Annotations) -> Samples:
    """Reads all .csv files in the directory provided, supposing they are SigDF from SeSAMe saved as csv files.

    :param datadir:  directory where sesame files are
    :type datadir: str | os.PathLike | MultiplexedPath

    :param annotation: Annotations object with genome version and array type corresponding to the data stored
    :type annotation: Annotations

    :return: a Samples object
    :rtype: Samples"""
    LOGGER.info(f'>> start reading sesame files')
    samples = Samples(None)
    samples.annotation = annotation

    # fin all .csv files in the subtree depending on datadir type
    file_list = get_files_matching(datadir, '*.csv')

    # load all samples
    for csv_file in file_list:
        sample = Sample.from_sesame(csv_file, annotation)
        samples.samples[sample.name] = sample

    LOGGER.info(f'done reading sesame files\n')
    return samples