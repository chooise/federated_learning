import copy
import random
from typing import List

import numpy as np

from loguru import logger

from Algorithm.Training_ASync import Training_ASync
from models import Aggregation, LocalUpdate_FedAvg
from utils.utils import getTrueLabels

COMM_BUDGET = 0.1
DECAY = 0.5


@logger.catch
class GitSFL(Training_ASync):
    def __init__(self, args, net_glob, dataset_train, dataset_test, dict_users, net_glob_client, net_glob_server):
        super().__init__(args, net_glob, dataset_train, dataset_test, dict_users)

        # GitSFL Setting
        self.repoSize = int(args.num_users * args.frac)
        self.repo = [copy.deepcopy(self.net_glob) for _ in range(self.repoSize)]
        self.modelServer = []
        self.modelClient = []
        self.modelVersion = [0 for _ in range(self.repoSize)]
        self.cumulative_label_distributions = [np.zeros(args.num_classes) for _ in range(self.repoSize)]
        self.cumulative_label_distribution_weight = [0 for _ in range(self.repoSize)]
        self.true_labels = getTrueLabels(self)
        self.selected_count = [0 for _ in range(args.num_users)]
        self.help_count = [0 for _ in range(args.num_users)]

        self.net_glob_client = net_glob_client
        self.net_glob_server = net_glob_server

        self.dataByLabel = self.organizeDataByLabel()

    @logger.catch()
    def train(self):
        init_users = np.random.choice(range(self.args.num_users), self.repoSize, replace=False)
        for model_index, client_index in enumerate(init_users):
            # [client_index, modelIndex, model_version, trainTime]
            self.update_queue.append([client_index, model_index, 0, self.clients.getTime(client_index)])
            self.idle_clients.remove(client_index)
            self.selected_count[client_index] += 1
        self.update_queue.sort(key=lambda x: x[-1])

        while self.time < self.args.limit_time:
            print("*" * 50)
            client_index, modelIndex, model_version, trainTime = self.update_queue.pop(0)
            self.modelVersion[modelIndex] += 1
            for update in self.update_queue:
                update[-1] -= trainTime
            self.time += trainTime
            self.cumulative_label_distribution_weight[modelIndex] = self.cumulative_label_distribution_weight[
                                                                        modelIndex] * DECAY + 1
            self.cumulative_label_distributions[modelIndex] = (self.cumulative_label_distributions[modelIndex] * DECAY +
                                                               self.true_labels[client_index]) / \
                                                              self.cumulative_label_distribution_weight[modelIndex]

            # self.splitTrain(client_index, modelIndex)
            self.tempTrain(client_index, modelIndex)

            self.Agg()

            self.test()

            self.weakAgg(modelIndex)

            nextClient = self.selectNextClient()
            self.update_queue.append([nextClient, modelIndex, model_version + 1, self.clients.getTime(nextClient)])
            self.update_queue.sort(key=lambda x: x[-1])
            self.selected_count[nextClient] += 1
            self.idle_clients.remove(nextClient)
            self.idle_clients.add(client_index)

            self.round += 1

        print(self.help_count)

    def splitTrain(self, curClient: int, modelIdx: int):
        helpers, provide_data = self.selectHelpers(curClient, modelIdx)
        # sampledData = [self.sampleData(helper, amount) for helper, amount in helpers.items()]

        pass

    def tempTrain(self, curClient: int, modelIdx: int):
        helpers, provide_data = self.selectHelpers(curClient, modelIdx)
        sampledData = self.sampleData(helpers, provide_data)
        sampledData.extend(self.dict_users[curClient])
        local = LocalUpdate_FedAvg(args=self.args, dataset=self.dataset_train, idxs=sampledData, verbose=False)
        w = local.train(round=self.round, net=copy.deepcopy(self.repo[modelIdx]).to(self.args.device))
        self.repo[modelIdx].load_state_dict(w)

    def Agg(self):
        # w_client = [copy.deepcopy(model_client.state_dict) for model_client in self.modelClient]
        # w_avg_client = Aggregation(w_client, self.modelVersion)
        # self.net_glob_client.load_state_dict(w_avg_client)
        #
        # w_server = [copy.deepcopy(model_server.state_dict) for model_server in self.modelClient]
        # w_avg_server = Aggregation(w_server, self.modelVersion)
        # self.net_glob_server.load_state_dict(w_avg_server)
        #
        # self.net_glob.load_state_dict(w_avg_client)
        # self.net_glob.load_state_dict(w_avg_server)

        #############################################
        w = [copy.deepcopy(model.state_dict()) for model in self.repo]
        w_avg = Aggregation(w, self.modelVersion)
        self.net_glob.load_state_dict(w_avg)

    def selectNextClient(self) -> int:
        nextClient = random.choice(list(self.idle_clients))
        return nextClient

    def weakAgg(self, modelIdx: int):
        # cur_model_client = self.modelClient[modelIdx]
        # w = [copy.deepcopy(self.net_glob_client.state_dict()), copy.deepcopy(cur_model_client)]
        # lens = [1, max(10 + self.modelVersion[modelIdx] - np.mean(self.modelVersion), 2)]
        # w_avg_client = Aggregation(w, lens)
        # cur_model_client.load_state_dict(w_avg_client)
        #
        # cur_model_server = self.modelServer[modelIdx]
        # w = [copy.deepcopy(self.net_glob_server.state_dict()), copy.deepcopy(cur_model_server)]
        # w_avg_server = Aggregation(w, lens)
        # cur_model_client.load_state_dict(w_avg_server)

        ###########################################################
        lens = [max(10 + self.modelVersion[modelIdx] - np.mean(self.modelVersion), 2), 1]
        w = [copy.deepcopy(self.repo[modelIdx].state_dict()), copy.deepcopy(self.net_glob.state_dict())]
        w_avg = Aggregation(w, lens)
        self.repo[modelIdx].load_state_dict(w_avg)

    def sampleData(self, helper: int, provideData: List[int]) -> List[int]:
        # randomSample
        sampledData = []
        for classIdx, num in enumerate(provideData):
            sampledData.extend(random.sample(self.dataByLabel[helper][classIdx], num))
        return sampledData

    def selectHelpers(self, curClient: int, modelIdx: int) -> tuple[int, List[int]]:
        overall_requirement = max(10, int(len(self.dict_users[curClient]) * COMM_BUDGET))
        cumulative_label_distribution = self.cumulative_label_distributions[modelIdx]
        prior_of_classes = [max(np.mean(cumulative_label_distribution) - label, 0)
                            for label in cumulative_label_distribution]
        requirement_classes = [int(overall_requirement * (prior / sum(prior_of_classes))) for prior in prior_of_classes]

        helpers = 200
        provide_data = []
        max_contribution = 0
        candidate = list(range(self.args.num_users))
        candidate.pop(curClient)
        random.shuffle(candidate)
        for client in candidate:
            contribution = 0
            temp = []
            for classIdx, label in enumerate(self.true_labels[client]):
                contribution += min(label, requirement_classes[classIdx])
                temp.append(min(label, requirement_classes[classIdx]))
            if contribution > max_contribution:
                max_contribution = contribution
                helpers = client
                provide_data = temp
        self.help_count[helpers] += 1

        print("overall_requirement:\t", overall_requirement)
        print("current_train_data:\t", list(self.true_labels[curClient]))
        print("cumu_label_distri:\t", list(map(int, cumulative_label_distribution)))
        print("prior_of_classes:\t", list(map(int, prior_of_classes)))
        print("required_classes:\t", requirement_classes)
        print("selected_helper:\t", list(self.true_labels[helpers]))
        print("total_provide_data:\t", provide_data)
        return helpers, provide_data

    def organizeDataByLabel(self) -> list[list[list[int]]]:
        organized = []
        for client in range(self.args.num_users):
            res = [[] for _ in range(self.args.num_classes)]
            all_local_data = self.dict_users[client]
            for data in all_local_data:
                res[self.dataset_train[data][1]].append(data)
            organized.append(res)
        return organized
