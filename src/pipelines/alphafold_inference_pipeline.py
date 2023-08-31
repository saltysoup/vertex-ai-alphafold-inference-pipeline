# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Universal Alphafold Inference Pipeline."""

from google_cloud_pipeline_components.v1.custom_job import create_custom_training_job_from_component
from kfp.v2 import dsl
from src import config
from src.components import  configure_run as ConfigureRunOp
from src.components import  data_pipeline
from src.components import  predict as PredictOp
from src.components import  relax as RelaxOp


DataPipelineOp = create_custom_training_job_from_component(
    data_pipeline,
    display_name='Data Pipeline',
    machine_type=config.DATA_PIPELINE_MACHINE_TYPE,
    nfs_mounts=[dict(
        server=config.NFS_SERVER,
        path=config.NFS_PATH,
        mountPoint=config.NFS_MOUNT_POINT)],
    network=config.NETWORK
)

WrappedPredictOp = create_custom_training_job_from_component(
    PredictOp,
    display_name='Predict',
    machine_type='g2-standard-12',
    accelerator_type='NVIDIA_L4',
    accelerator_count=1
)

WrappedRelaxOp = create_custom_training_job_from_component(
    RelaxOp,
    display_name='Relax',
    machine_type='g2-standard-12',
    accelerator_type='NVIDIA_L4',
    accelerator_count=1
)

@dsl.pipeline(
    name='alphafold-inference-pipeline',
    description='AlphaFold inference using original data pipeline.'
)
def alphafold_inference_pipeline(
    sequence_path: str,
    project: str,
    region: str,
    max_template_date: str,
    model_preset: str = 'monomer',
    use_small_bfd: bool = True,
    num_multimer_predictions_per_model: int = 5,
    is_run_relax: str = 'relax'
):
  """Universal Alphafold Inference Pipeline."""
  run_config = ConfigureRunOp(
      sequence_path=sequence_path,
      model_preset=model_preset,
      num_multimer_predictions_per_model=num_multimer_predictions_per_model,
  ).set_display_name('Configure Pipeline Run')

  model_parameters = dsl.importer(
      artifact_uri=config.MODEL_PARAMS_GCS_LOCATION,
      artifact_class=dsl.Artifact,
      reimport=True
  ).set_display_name('Model parameters')

  reference_databases = dsl.importer(
      artifact_uri=config.NFS_MOUNT_POINT,
      artifact_class=dsl.Dataset,
      reimport=False,
      metadata={
          'uniref90': config.UNIREF90_PATH,
          'mgnify': config.MGNIFY_PATH,
          'bfd': config.BFD_PATH,
          'small_bfd': config.SMALL_BFD_PATH,
          'uniref30': config.UNIREF30_PATH,
          'pdb70': config.PDB70_PATH,
          'pdb_mmcif': config.PDB_MMCIF_PATH,
          'pdb_obsolete': config.PDB_OBSOLETE_PATH,
          'pdb_seqres': config.PDB_SEQRES_PATH,
          'uniprot': config.UNIPROT_PATH,
          }
  ).set_display_name('Reference databases')

  data_pipeline = DataPipelineOp(
      project=project,
      location=region,
      ref_databases=reference_databases.output,
      sequence=run_config.outputs['sequence'],
      max_template_date=max_template_date,
      run_multimer_system=run_config.outputs['run_multimer_system'],
      use_small_bfd=use_small_bfd,
  ).set_display_name('Prepare Features')

  with dsl.ParallelFor(
        loop_args=run_config.outputs['model_runners'], 
        parallelism=config.PARALLELISM
        ) as model_runner:
    model_predict = WrappedPredictOp(
        project=project,
        location=region,        
        model_features=data_pipeline.outputs['features'],
        model_params=model_parameters.output,
        model_name=model_runner.model_name,
        prediction_index=model_runner.prediction_index,
        run_multimer_system=run_config.outputs['run_multimer_system'],
        num_ensemble=run_config.outputs['num_ensemble'],
        random_seed=model_runner.random_seed
    )
    #model_predict.add_node_selector_constraint(
    #    config.GKE_ACCELERATOR_KEY, config.GPU_TYPE)
    model_predict.set_env_variable(
        'TF_FORCE_UNIFIED_MEMORY', config.TF_FORCE_UNIFIED_MEMORY)
    model_predict.set_env_variable(
        'XLA_PYTHON_CLIENT_MEM_FRACTION', 
        config.XLA_PYTHON_CLIENT_MEM_FRACTION)
    model_predict.set_retry(
                        num_retries=1,
                        backoff_duration="60s",
                        backoff_factor=2,
                    )

    with dsl.Condition(is_run_relax == 'relax'):
      relax_protein = WrappedRelaxOp(
        project=project,
        location=region,
        unrelaxed_protein=model_predict.outputs['unrelaxed_protein'],
        use_gpu=True,
      )
      #relax_protein.add_node_selector_constraint(
      #  config.GKE_ACCELERATOR_KEY, config.RELAX_GPU_TYPE)
      relax_protein.set_env_variable(
        'TF_FORCE_UNIFIED_MEMORY', config.TF_FORCE_UNIFIED_MEMORY)
      relax_protein.set_env_variable(
        'XLA_PYTHON_CLIENT_MEM_FRACTION', 
        config.XLA_PYTHON_CLIENT_MEM_FRACTION)
      relax_protein.set_retry(
                        num_retries=1,
                        backoff_duration="60s",
                        backoff_factor=2,
                    ) 
