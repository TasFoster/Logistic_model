import json
from pydantic import BaseModel, model_validator
from typing import Literal

class Pos(BaseModel):
    x: int
    y: int

class Scale(BaseModel):
    width: int
    height: int

class Effecive_el(BaseModel):
    name: str
    ef: float

class Node_data(BaseModel):
    id: int
    name: str
    type_node: str
    input_list: dict[str,float]
    output_list: dict[str,float]
    effecive_ellements: list[Effecive_el]
    pos: Pos
    scale: Scale

class Rib_data(BaseModel):
    node_in_id: int
    node_out_id: int
    storage: int
    type_el: str    


class Graph_data(BaseModel):
    nodes: list[Node_data]
    ribs: list[Rib_data]
    input_stream: int
    type_input: str
    start_node_id: int
    type_output: str
    end_node_id: int

    def sort_rib(self, mode: Literal["in", "out"] = "in"):
        if mode == "in":
            list_rib = self.ribs.sort(key=lambda x: x.node_in_id)
        else:
            list_rib = self.ribs.sort(key=lambda x: x.node_out_id)
        return list_rib
    
    def find_node(self, id:int):
        return self.nodes[id - 1];
    
    def check_types_rib(self, mode:Literal["in", "out", "all"] = "in"):
        self.sort_rib(mode = mode)
        current_node_id = 0
        current_node_list = []
        for i in self.ribs:
            if mode == "in":
                node_id = i.node_in_id
            else:
                if current_node_id == 0:
                    current_node_id += 1
                node_id = i.node_out_id
            if node_id != current_node_id:
                if node_id - current_node_id != 1:
                        raise ValueError(f"Наушена индексация начиная с id={current_node_id}")
                current_node_id = node_id
                if mode == "in":
                    current_node_list = [ j for j in self.find_node(current_node_id).output_list]
                else:
                    current_node_list = [ j for j in self.find_node(current_node_id).input_list]
            if i.type_el in current_node_list:
                current_node_list.remove(i.type_el)
            else:
                raise ValueError(f"Узел {self.find_node(current_node_id).name} ошибка типизации.")

    @model_validator(mode="after")
    def check_graph(self):
        for i in self.nodes:
            if len(i.output_list) + len(i.input_list) <= 0:
                raise ValueError(f"Узел {i.name} не связан ни с одним ребром.")
            self.check_types_rib("in")
            self.check_types_rib("out")

def ReadJson():
    with open("config/example_graph.json", "r", encoding="utf-8") as file:
        data = json.load(file)
    graph = Graph_data.model_validate(data)
    return graph

ReadJson()