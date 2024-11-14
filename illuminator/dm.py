"""
This module defines the functions used to compute DMR (Differentially Methylated Regions) and DMP (Differentially
Methylated Probes).
"""
import numpy as np
import pandas as pd
import pyranges as pr

from patsy import dmatrix
from scipy.stats import combine_pvalues
from statsmodels.api import OLS
from statsmodels.stats.multitest import multipletests
from joblib import Parallel, delayed

from illuminator.annotations import Annotations
from illuminator.utils import remove_probe_suffix, set_level_as_index

from illuminator.utils import get_logger

LOGGER = get_logger()


def combine_p_values_stouffer(p_values: pd.Series) -> np.ndarray:
    """shortcut to scipy's function, using Stouffer method to combine p-values. Only return the combined p-value

    :param p_values: p-values to combine
    :type p_values: pandas.Series

    :return: numpy array of combined p-values
    :rtype: numpy.ndarray"""
    return combine_pvalues(p_values, method='stouffer')[1]

def get_model_parameters(betas_values, design_matrix: pd.DataFrame, factor_names: list[str]) -> list[float]:
    """Create an Ordinary Least Square model for the betas values, using the design matrix provided, fit it and
    extract the required results for DMP detection (p-value, t-value, estimate, standard error)

    :param betas_values: beta values to fit
    :type betas_values: array-like
    :param design_matrix: design matrix for the model
    :type design_matrix: pandas.DataFrame
    :param factor_names: factors used in the model
    :type factor_names: list[str]

    :return: p-value, t-value, estimate, standard error
    :rtype: list[float]"""
    fitted_ols = OLS(betas_values, design_matrix, missing='drop').fit()  # drop NA values
    results = [fitted_ols.f_pvalue]
    for factor in factor_names:
        results.extend([fitted_ols.tvalues[factor], fitted_ols.params[factor], fitted_ols.bse[factor]])
    return results


def get_dmp(betas: pd.DataFrame, formula: str, sample_sheet: pd.DataFrame, keep_na=True) -> pd.DataFrame | None:
    """Find Differentially Methylated Probes (DMP)

    More info on  design matrices and formulas:
        - https://www.statsmodels.org/devel/gettingstarted.html
        - https://patsy.readthedocs.io/en/latest/overview.html

    :param betas: beta values of all the samples to use to find DMRs, as returned by Samples.get_betas()
    :type betas: pandas.DataFrame
    :param formula: R-like formula used in the design matrix to describe the statistical model. e.g. '~age + sex'
    :type formula: str
    :param sample_sheet: metadata used in the model, typically a samplesheet. It must have the samples' names in a
        column called `sample_name` and the column(s) used in the formula (e.g. ['age', 'sex'])
    :type sample_sheet: pandas.DataFrame
    :param keep_na: keep probes that have NA values in the beta matrix. Default True
    :type keep_na: bool

    :return: dataframe with probes as rows and p_vales and model estimates in columns
    :rtype: pandas.DataFrame
    """

    LOGGER.info(f'>>> Start get DMP')

    # check the input
    if 'sample_name' not in sample_sheet.columns:
        LOGGER.error('get_dmp() :  dataframe sample_sheet must have a sample_name column')
        return None

    # drop any probe that has a NA beta value
    if not keep_na:
        betas = betas.dropna()

    # data init.
    betas = set_level_as_index(betas, 'probe_id', drop_others=True)

    # make the design matrix
    sample_info = sample_sheet.set_index('sample_name')
    design_matrix = dmatrix(formula, sample_info, return_type='dataframe')

    # remove the intercept from the factors if it exists
    factor_names = [f for f in design_matrix.columns if 'intercept' not in f.lower()]

    # derive output columns' names for factors' names
    column_names = ['p_value']
    for factor in factor_names:
        column_names.extend([f't_value_{factor}', f'estimate_{factor}', f'std_err_{factor}'])

    # if it's a small dataset, don't parallelize
    if len(betas) <= 10000:
        result_array = [get_model_parameters(row[1:], design_matrix, factor_names) for row in betas.itertuples()]
    # otherwise parallelize
    else:
        def wrapper_get_model_parameters(row):
            return get_model_parameters(row, design_matrix, factor_names)
        result_array = Parallel(n_jobs=-1)(delayed(wrapper_get_model_parameters)(row[1:]) for row in betas.itertuples())

    LOGGER.info(f'get DMP done')

    return pd.DataFrame(result_array, index=betas.index, columns=column_names, dtype='float64')


def get_dmr(betas: pd.DataFrame, annotation: Annotations, dmp: pd.DataFrame,
            dist_cutoff: float | None = None, seg_per_locus: float = 0.5) -> pd.DataFrame:
    """Find Differentially Methylated Regions (DMR) based on euclidian distance between Beta values

    :param betas: beta values of all the samples to use to find DMRs, as returned by Samples.get_betas()
    :type betas: pandas.DataFrame
    :param annotation: samples' annotation information
    :type annotation: Annotations
    :param dmp: p-values and statistics for each probe, as returned by get_dmp()
    :type dmp: pandas.DataFrame
    :param dist_cutoff: cutoff used to find change points between DMRs, used on euclidian distance between beta values.
        If set to None (default) will be calculated depending on `seg_per_locus` parameter value. Default: None
    :type dist_cutoff: float | None
    :param seg_per_locus: used if dist_cutoff is not set : defines what quartile should be used as a distance cut-off.
        Higher values leads to more segments. Should be 0 < seg_per_locus < 1. Default: 0.5.
    :type seg_per_locus: float
    :type seg_per_locus: float

    :return: dataframe with DMRs
    :rtype: pandas.DataFrame
    """

    LOGGER.info(f'>>> Start get DMR')

    # data init.
    betas = set_level_as_index(betas, 'probe_id', drop_others=True)
    betas = betas.drop(columns=['type', 'channel', 'probe_type', 'index'], errors='ignore')

    # get genomic range information (for chromosome id and probe position)
    probe_coords_df = annotation.genomic_ranges.drop(columns='Strand', errors='ignore')
    non_empty_coords_df = probe_coords_df[probe_coords_df.End > probe_coords_df.Start]  # remove 0-width ranges

    betas_no_na = betas.dropna()  # remove probes with missing values
    cpg_ids = non_empty_coords_df.join(betas_no_na, how='inner')

    # if there was no match, try again after trimming the suffix from the genomic ranges probe IDs
    if len(cpg_ids) == 0:
        non_empty_coords_df.index = non_empty_coords_df.index.map(remove_probe_suffix)
        cpg_ids = non_empty_coords_df.join(betas_no_na)

    if len(cpg_ids) == 0:
        LOGGER.error('No match found between genomic probe coordinates and beta probe IDs')
        return pd.DataFrame()

    # sort ranges and identify last probe of each chromosome
    # cpg_ranges = pr.PyRanges(cpg_ids).sort_ranges(natsorting=True)  # to have the same sorting as sesame
    cpg_ranges = pr.PyRanges(cpg_ids).sort_ranges()
    next_chromosome = cpg_ranges['Chromosome'].shift(-1)
    last_probe_in_chromosome = cpg_ranges['Chromosome'] != next_chromosome

    # compute Euclidian distance of beta values between two consecutive probes of each sample
    sample_names = betas.columns
    beta_euclidian_dist = (cpg_ranges[sample_names].diff(-1) ** 2).sum(axis=1)
    beta_euclidian_dist.iloc[-1] = None  # last probe shouldn't have a distance (default is 0 otherwise)

    # determine cut-off if not provided
    if dist_cutoff is None:
        if not 0 < seg_per_locus < 1:
            LOGGER.warning(f'Invalid parameter `seg_per_locus` {seg_per_locus}, should be in ]0:1[. Setting it to 0.5')
            seg_per_locus = 0.5
        # dist_cutoff = np.quantile(beta_euclidian_dist.dropna(), 1 - seg_per_locus)  # sesame (keep last probes)
        dist_cutoff = np.quantile(beta_euclidian_dist[~last_probe_in_chromosome], 1 - seg_per_locus)
        LOGGER.debug(f'Segments per locus : {seg_per_locus}')

    if dist_cutoff <= 0:
        LOGGER.warning(f'Euclidian distance cutoff for DMP should be > 0')
    LOGGER.debug(f'Euclidian distance cutoff for DMP : {dist_cutoff}')

    # find change points
    change_points = last_probe_in_chromosome | (beta_euclidian_dist > dist_cutoff)

    # give a unique ID to each segment
    segment_id = change_points.shift(fill_value=True).cumsum()
    segment_id.name = 'segment_id'

    # merging segments with all probes - including empty ones dropped at the beginning
    segments = probe_coords_df.loc[betas.index].join(segment_id).sort_values('segment_id')

    last_segment_id = segment_id.max()
    LOGGER.info(f'Number of segments : {last_segment_id}')

    # assign new segments IDs to NA segments
    na_segments_indexes = segments.segment_id.isna()
    nb_na_segments = na_segments_indexes.sum()
    if nb_na_segments > 0:
        LOGGER.info(f'Adding {nb_na_segments} NA segments')
        segments.loc[na_segments_indexes, 'segment_id'] = [n for n in range(nb_na_segments)] + last_segment_id + 1
        segments.segment_id = segments.segment_id.astype(int)

    # combine probes p-values with segments information
    dmr = segments.join(dmp)

    # group segments by ID to compute DMR values
    segments_grouped = dmr.groupby('segment_id')

    # get each segment's start and end
    dmr['segment_start'] = segments_grouped['Start'].transform('min')
    dmr['segment_end'] = segments_grouped['End'].transform('max')

    # calculate each segment's p-values
    LOGGER.info('combining p-values, it might take a few minutes...')
    dmr['segment_p_value'] = segments_grouped['p_value'].transform(combine_p_values_stouffer)
    nb_significant = len(dmr.loc[dmr.segment_p_value < 0.05, 'segment_id'].drop_duplicates())
    LOGGER.info(f' - {nb_significant} significant segments (p-value < 0.05)')

    # use Benjamini/Hochberg's method to adjust p-values
    idxs = ~np.isnan(dmr.segment_p_value)  # any NA in segment_p_value column causes BH method to crash
    dmr.loc[idxs, 'segment_p_value_adjusted'] = multipletests(dmr.loc[idxs, 'segment_p_value'], method='fdr_bh')[1]
    nb_significant = len(dmr.loc[dmr.segment_p_value_adjusted < 0.05, 'segment_id'].drop_duplicates())
    LOGGER.info(f' - {nb_significant} significant segments after Benjamini/Hochberg\'s adjustment (p-value < 0.05)')

    # calculate estimates' means for each factor
    for c in dmp.columns:
        if c.startswith('estimate_'):
            dmr[f'segment_{c}'] = segments_grouped[c].transform('mean')

    return dmr