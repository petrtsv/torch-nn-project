import json

from models.images.classification.few_shot_learning.mctdfmn import *


def change_dataset(model_folder: str, dataset_name: str, record: int):
    model_file = os.path.join(model_folder, 'output', 'trained_model_state_dict.tar')
    info_file = os.path.join(model_folder, 'output', 'info.json')
    with open(info_file) as fin:
        info = json.load(fin)
    print(info)
    model = torch.load(model_file)
    model.eval()

    dataset = LABELED_DATASETS[dataset_name](augment_prob=0, image_size=info['image_size']).subdataset
    sampler = FSLEpisodeSampler(subdataset=dataset, n_way=info['n_way'], n_shot=info['n_shot'],
                                batch_size=info['val_batch_size'],
                                balanced=info['balanced_batches'])
    info['dataset'] += '->' + dataset_name
    info['record'] = record
    print(info['dataset'])

    score = evaluate_solution(model, sampler)
    info['accuracy'] = score

    # info.pop('name')
    info.pop('comment')
    session = Session()
    session.build(name=info['screen_name'] + '_transfer',
                  comment=r"Few-Shot Learning solution from '" + info['full_name'] + "'",
                  **info)
    torch.save(model, os.path.join(session.data['output_dir'], "trained_model_state_dict.tar"))
    session.save_info()
    # print(evaluate_solution(model, val_sampler, n_iterations=600))


if __name__ == '__main__':
    # path = "D:\\petrtsv\\projects\\ds\\pytorch-sessions\\FSL_MCTDFMN\\FSL_MCTDFMN_055670-43-55-20-05-04-2020"
    paths = [r'D:\petrtsv\projects\ds\pytorch-sessions\FSL_MCTDFMN\FSL_MCTDFMN_637276-53-47-18-06-04-2020',  # 1-shot
             r'D:\petrtsv\projects\ds\pytorch-sessions\FSL_MCTDFMN\FSL_MCTDFMN_055670-43-55-20-05-04-2020',  # 5-shot
             ]

    DATASET_NAME = 'cub'
    RECORD = 110

    for path in paths:
        print(path)
        change_dataset(path, DATASET_NAME, RECORD)
        print()