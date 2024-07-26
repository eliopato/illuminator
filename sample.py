from pathlib import Path
import logging
import pandas as pd
import numpy as np

from idat import IdatDataset
from annotations import Annotations, Channel
from sample_sheet import SampleSheet

LOGGER = logging.getLogger(__name__)


class Samples:

    def __init__(self, sheet: SampleSheet, annotation: Annotations):
        self.annotation = annotation
        self.sheet = sheet
        self.samples = {}

    def read_samples(self, datadir: str, max_samples: int | None = None) -> None:

        LOGGER.info(f'>> start reading sample files from {datadir}')

        for _, line in self.sheet.df.iterrows():
            sample = Sample()
            for channel in Channel:
                pattern = f'*{line.sentrix_id}*{line.sentrix_position}*{channel}*.idat'
                paths = [p.__str__() for p in Path(datadir).rglob(pattern)]
                if len(paths) == 0:
                    LOGGER.error(f'no paths found matching {pattern}')
                    continue
                if len(paths) > 1:
                    LOGGER.error(f'Too many files found matching {pattern} : {paths}')
                    continue
                LOGGER.info(f'reading file {paths[0]}')
                sample.set_idata(channel, IdatDataset(paths[0]))
            self.samples[line.sample_name] = sample
            if max_samples is not None and len(self.samples) == max_samples:
                break

        LOGGER.info(f'reading sample files done\n')

    def merge_manifest_info(self) -> None:
        LOGGER.info(f'>> start merging manifest and sample data frames')
        for name, sample in self.samples.items():
            LOGGER.info(f'merging sample {name}')
            sample.merge_manifest_info(self.annotation)
        LOGGER.info(f'done merging manifest and sample data frames\n')

    def drop_optional_cols(self) -> None:
        for sample in self.samples.values():
            sample.drop_optional_cols()


class Sample:

    def __init__(self):
        self.idata = dict()
        self.df = None
        self.full_df = None

    def set_idata(self, channel: Channel, dataset: IdatDataset) -> None:
        self.idata[channel] = dataset

    def merge_manifest_info(self, annotation: Annotations) -> None:
        """ Merge manifest and mask dataframes to idat information."""

        channel_dfs = []

        for channel, idata in self.idata.items():
            manifest_df = annotation.manifest
            mask_df = annotation.mask
            idata_df = idata.probe_means

            sample_df = idata_df.join(manifest_df, how='inner')
            sample_df = sample_df.join(mask_df.drop(columns='probe_type'), on='probe_id', how='left')
            lost_probes = len(sample_df) - len(idata_df)
            if lost_probes > 0:
                LOGGER.warning(f'Lost {lost_probes} while merging information with Manifest (out of {len(idata_df)})')

            # deduce methylation state (M = methylated, U = unmethylated) depending on infinium type
            sample_df['methylation_state'] = '?'
            sample_df.loc[sample_df.type == 'II', 'methylation_state'] = 'M' if channel.is_green else 'U'
            sample_df.loc[(sample_df.type == 'I') & (sample_df.index == sample_df.address_b), 'methylation_state'] = 'M'
            sample_df.loc[(sample_df.type == 'I') & (sample_df.index == sample_df.address_a), 'methylation_state'] = 'U'

            # deduce which Type I probes are out-of-band (oob) and which ones are in-band (ib) by comparing the channel
            # specified in the manifest with the file channel. Type II probes are set in-band.
            sample_df['signal_channel'] = channel.value[0]
            sample_df['ib_or_oob'] = 'ib'
            oob_indexes = (sample_df.type == 'I') & (sample_df.channel != sample_df.signal_channel)
            sample_df.loc[oob_indexes, 'ib_or_oob'] = 'oob' + sample_df.loc[oob_indexes, 'signal_channel']
            sample_df = sample_df.rename(columns={'channel': 'manifest_channel'})

            # to improve readability
            sample_df.loc[sample_df.probe_type == 'rs', 'probe_type'] = 'snp'

            channel_dfs.append(sample_df)

        self.full_df = pd.concat(channel_dfs)
        self.df = self.drop_optional_cols()

    def get_betas(self):
        # todo check why sesame gives the option to calculate separately R/G channels for Type I probes (sum.TypeI arg)
        # https://github.com/zwdzwd/sesame/blob/261e811c5adf3ec4ecc30cdf927b9dcbb2e920b6/R/sesame.R#L191
        pivoted = self.df.pivot(values='mean_value', columns=['signal_channel', 'methylation_state'], index='probe_id')
        pivoted = pivoted.fillna(0)
        methylated_signal = pivoted['R', 'M'] + pivoted['G', 'M']
        unmethylated_signal = pivoted['R', 'U'] + pivoted['G', 'U']
        # set minimum values for each term as set in sesame
        pivoted['beta'] = methylated_signal.clip(lower=1) / (methylated_signal + unmethylated_signal).clip(lower=2)

    def infer_typeI_channel(self, switch_failed=False, mask_failed=False) -> None:
        LOGGER.info(' >> starting to infer probes')
        pivoted = self.df[self.df.type == 'I'].pivot(values='mean_value',
                                                     columns=['signal_channel', 'methylation_state'],
                                                     index=['probe_id', 'manifest_channel'])

        # infer new column values from maximum intensities
        red_max = pivoted['R'].max(axis=1)
        grn_max = pivoted['G'].max(axis=1)
        pivoted['inferred_channel'] = np.where(red_max > grn_max, 'R', 'G')

        # handle failed probes
        if not switch_failed:
            # Calculate background max
            bg_signal_values = np.concatenate([pivoted.loc[(pivoted.inferred_channel == 'R'), 'G'],
                                               pivoted.loc[(pivoted.inferred_channel == 'G'), 'R']])
            bg_max = np.percentile(bg_signal_values, 95)
            # reset color channel to the value of 'manifest_channel' for failed indexes
            failed_idxs = (red_max.isna() | grn_max.isna() | (red_max.combine(grn_max, max) < bg_max))
            pivoted.loc[failed_idxs, 'inferred_channel'] = pivoted[failed_idxs].index.get_level_values(1).tolist()

        # todo
        # if mask_failed:
        #     sdf.loc[inf1_idx[idx], 'mask'] = True

        # update initial dataframes
        inferred_channel_df = pivoted.reset_index('manifest_channel')['inferred_channel']
        self.df = self.df.merge(inferred_channel_df, on='probe_id', how='left')
        self.full_df = self.full_df.merge(inferred_channel_df, on='probe_id', how='left')

        LOGGER.info(pivoted['inferred_channel'].reset_index().groupby(['manifest_channel', 'inferred_channel']).count())

    def drop_optional_cols(self) -> pd.DataFrame | None:
        """ Clear up the data frame to make it easier to read and only keep track of required values"""
        if self.full_df is None:
            LOGGER.warning('drop_optional_cols : please run merge_manifest_info() first')
            return None

        required_cols = ['probe_id', 'type', 'probe_type', 'manifest_channel', 'signal_channel', 'methylation_state',
                         'ib_or_oob', 'mean_value']
        missing_cols = [c for c in required_cols if c not in self.full_df.columns]
        if len(missing_cols) > 0:
            LOGGER.warning(f'drop_optional_cols : not all required cols where found in df. Missing cols {missing_cols}')
            return None

        return self.full_df[required_cols]
