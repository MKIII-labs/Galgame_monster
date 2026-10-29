# -*- coding: utf-8 -*-
"""数学竞技场：大学数学的「一局五题」训练游戏。

设计目标与图寻一致：一局一题、可比较、立刻揭晓、知识会累积。
但大学数学更重概念与反例，所以题型是：
  calc    计算题（数值答案，相对误差 <=1% 判对）
  tf      真假判断
  counter 反例选择

题库由「程序生成的计算题 + 人工校验的概念题」组成，
不调用模型生成题目，避免一本正经地出错。
"""
import math
import copy
import random
import re
import threading
import time
import uuid
import zlib
from fractions import Fraction

from app.math import abilities as math_abilities
from app.math.constants import (
    ROUND_TTL_SEC, MAX_AI, QUESTION_COUNT,
    SUBJECT_NAMES, COURSE_NAMES, COURSE_PARENT, COURSE_ORDER,
    AI_PLAYERS, HUMAN_ID,
)
from app.math.errors import MathError

# ------------------------------------------------------------
# 概念题（人工校验）：每题都带答案、解释与知识点
# ------------------------------------------------------------

CONCEPTS = [
    # ---------------- 数学分析 ----------------
    {"subject": "analysis", "kind": "tf", "difficulty": 2,
     "prompt": "若函数 f 在 x0 可导，则 f 在 x0 连续。",
     "answer": True, "skill": "连续与可导",
     "explain": "可导必连续：f(x)-f(x0) = [f(x)-f(x0)]/(x-x0) · (x-x0) → f'(x0)·0 = 0。",
     "ai_hint": "可导是更强的条件，导数存在就说明差商有极限，函数值必须趋于 f(x0)。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 2,
     "prompt": "若函数 f 在 x0 连续，则 f 在 x0 可导。",
     "answer": False, "skill": "连续与可导",
     "explain": "反例：f(x)=|x| 在 x=0 连续，但左右导数分别为 -1 和 1，不可导。",
     "ai_hint": "连续只保证函数值不断，不保证差商极限存在；尖点就是反例。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 2,
     "prompt": "单调有界实数列必收敛。",
     "answer": True, "skill": "数列极限",
     "explain": "单调递增且有上界时，上确界就是极限；单调递减同理。",
     "ai_hint": "单调 + 有界是实数列收敛的充分条件，这是实数完备性的直接推论。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 2,
     "prompt": "有界实数列必收敛。",
     "answer": False, "skill": "数列极限",
     "explain": "反例：a_n=(-1)^n 有界，但在 -1 与 1 之间振荡，不收敛。",
     "ai_hint": "有界只排除了发散到无穷，仍可能来回振荡。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 3,
     "prompt": "若级数 ∑a_n 收敛，则 a_n→0。",
     "answer": True, "skill": "级数",
     "explain": "部分和 S_n 收敛，则 a_n=S_n-S_{n-1}→0。",
     "ai_hint": "收敛级数的通项必须趋于 0，这是必要条件。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 3,
     "prompt": "若 a_n→0，则级数 ∑a_n 收敛。",
     "answer": False, "skill": "级数",
     "explain": "反例：调和级数 ∑1/n 发散，但 1/n→0。",
     "ai_hint": "通项趋于 0 只是必要条件，不是充分条件；调和级数是最经典的反例。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 3,
     "prompt": "若 f 在 [a,b] 上连续，则 f 在 [a,b] 上黎曼可积。",
     "answer": True, "skill": "黎曼积分",
     "explain": "闭区间上的连续函数一致连续，因此可用振幅和证明可积。",
     "ai_hint": "连续函数在闭区间上一定可积，这是黎曼积分的基本定理之一。"},
    {"subject": "analysis", "kind": "tf", "difficulty": 3,
     "prompt": "若 f 在 [a,b] 上黎曼可积，则 f 在 [a,b] 上连续。",
     "answer": False, "skill": "黎曼积分",
     "explain": "反例：阶跃函数在 [0,1] 上可积，但在跳变点不连续。",
     "ai_hint": "可积比连续弱得多；有限个不连续点的有界函数仍可积。"},
    {"subject": "analysis", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个是「连续但不可导」的例子？",
     "options": [
         {"id": "a", "text": "f(x)=|x| 在 x=0"},
         {"id": "b", "text": "f(x)=x² 在 x=0"},
         {"id": "c", "text": "f(x)=sin x 在 x=0"},
         {"id": "d", "text": "f(x)=e^x 在 x=0"},
     ],
     "answer": "a", "skill": "连续与可导",
     "explain": "|x| 在 0 点连续，但左导数为 -1、右导数为 1，不可导。",
     "ai_hint": "尖点是最典型的连续不可导例子。"},
    {"subject": "analysis", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个是「收敛但不单调」的数列？",
     "options": [
         {"id": "a", "text": "a_n=(-1)^n/n（n≥1）"},
         {"id": "b", "text": "a_n=1/n"},
         {"id": "c", "text": "a_n=n"},
         {"id": "d", "text": "a_n=1"},
     ],
     "answer": "a", "skill": "数列极限",
     "explain": "(-1)^n/n 收敛到 0，但正负交替，不单调。",
     "ai_hint": "收敛不要求单调，只要项最终靠近极限。"},
    {"subject": "analysis", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个是「有界但发散」的数列？",
     "options": [
         {"id": "a", "text": "a_n=(-1)^n"},
         {"id": "b", "text": "a_n=1/n"},
         {"id": "c", "text": "a_n=n"},
         {"id": "d", "text": "a_n=0"},
     ],
     "answer": "a", "skill": "数列极限",
     "explain": "(-1)^n 始终在 ±1 之间，有界，但两个子列极限不同，因此发散。",
     "ai_hint": "有界 + 振荡 = 发散。"},
    {"subject": "analysis", "kind": "counter", "difficulty": 3,
     "prompt": "下列哪个点满足 f'(x0)=0，但不是极值点？",
     "options": [
         {"id": "a", "text": "f(x)=x³，x0=0"},
         {"id": "b", "text": "f(x)=x²，x0=0"},
         {"id": "c", "text": "f(x)=-x²，x0=0"},
         {"id": "d", "text": "f(x)=|x|，x0=0"},
     ],
     "answer": "a", "skill": "导数应用",
     "explain": "x³ 在 0 点导数为 0，但函数单调递增，0 不是极值点。",
     "ai_hint": "驻点不一定是极值点，x³ 是标准反例。"},

    # ---------------- 高等代数 ----------------
    {"subject": "algebra", "kind": "tf", "difficulty": 2,
     "prompt": "两个可逆矩阵之和一定可逆。",
     "answer": False, "skill": "矩阵运算",
     "explain": "反例：I 与 -I 都可逆，但 I+(-I)=0 不可逆。",
     "ai_hint": "可逆矩阵的集合对加法不封闭。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 2,
     "prompt": "若 AB=0，则 A=0 或 B=0。",
     "answer": False, "skill": "矩阵运算",
     "explain": "反例：A=[[1,0],[0,0]]，B=[[0,0],[1,0]]，AB=0 但两者都不为零。",
     "ai_hint": "矩阵有零因子，不能用实数乘法的消去律。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 3,
     "prompt": "相似矩阵有相同的特征多项式。",
     "answer": True, "skill": "特征值",
     "explain": "若 B=P^{-1}AP，则 det(λI-B)=det(P^{-1}(λI-A)P)=det(λI-A)。",
     "ai_hint": "相似变换不改变特征多项式。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 3,
     "prompt": "特征多项式相同的两个矩阵一定相似。",
     "answer": False, "skill": "特征值",
     "explain": "反例：I 与 [[1,1],[0,1]] 的特征多项式都是 (λ-1)²，但二者不相似。",
     "ai_hint": "特征多项式相同只能保证特征值相同，不能保证可对角化程度相同。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 3,
     "prompt": "实对称矩阵一定可对角化。",
     "answer": True, "skill": "对角化",
     "explain": "实对称矩阵的特征值全为实数，且不同特征值的特征向量正交，可正交对角化。",
     "ai_hint": "谱定理：实对称矩阵可以正交对角化。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 3,
     "prompt": "n 阶方阵若有 n 个线性无关的特征向量，则一定可对角化。",
     "answer": True, "skill": "对角化",
     "explain": "把这 n 个特征向量作为列组成可逆矩阵 P，则 P^{-1}AP 为对角矩阵。",
     "ai_hint": "有足够多的线性无关特征向量，就是可对角化的定义。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 3,
     "prompt": "矩阵的秩等于非零特征值的个数（计重数）。",
     "answer": False, "skill": "秩与特征值",
     "explain": "反例：[[0,1],[0,0]] 的秩为 1，但两个特征值都是 0。",
     "ai_hint": "幂零矩阵的秩可以大于非零特征值个数。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 4,
     "prompt": "若 A²=A，则 A 一定可对角化。",
     "answer": True, "skill": "对角化",
     "explain": "A 的极小多项式整除 x(x-1)，无重根，因此可对角化。",
     "ai_hint": "幂等矩阵的特征值只能是 0 或 1，且极小多项式无重根。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 2,
     "prompt": "行列式为零的方阵一定不可逆。",
     "answer": True, "skill": "行列式",
     "explain": "方阵可逆当且仅当行列式非零。",
     "ai_hint": "行列式是判断可逆性最直接的工具。"},
    {"subject": "algebra", "kind": "tf", "difficulty": 2,
     "prompt": "齐次线性方程组有非零解，当且仅当系数矩阵的秩小于未知数个数。",
     "answer": True, "skill": "线性方程组",
     "explain": "解空间维数 = n - rank(A)；有非零解等价于维数 > 0。",
     "ai_hint": "秩 < 未知数个数时，自由变量存在，就有非零解。"},
    {"subject": "algebra", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个矩阵满足 A²=0，但 A≠0？",
     "options": [
         {"id": "a", "text": "[[0,1],[0,0]]"},
         {"id": "b", "text": "[[1,0],[0,1]]"},
         {"id": "c", "text": "[[1,0],[0,0]]"},
         {"id": "d", "text": "[[0,0],[0,0]]"},
     ],
     "answer": "a", "skill": "矩阵运算",
     "explain": "[[0,1],[0,0]]²=0，但矩阵本身非零。",
     "ai_hint": "幂零矩阵是 A²=0 但 A≠0 的典型例子。"},
    {"subject": "algebra", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪组矩阵都可逆，但它们的和不可逆？",
     "options": [
         {"id": "a", "text": "I 与 -I"},
         {"id": "b", "text": "I 与 I"},
         {"id": "c", "text": "2I 与 3I"},
         {"id": "d", "text": "I 与 2I"},
     ],
     "answer": "a", "skill": "矩阵运算",
     "explain": "I 与 -I 都可逆，但 I+(-I)=0 不可逆。",
     "ai_hint": "找一对互为相反数的可逆矩阵即可。"},
    {"subject": "algebra", "kind": "counter", "difficulty": 3,
     "prompt": "下列哪组矩阵特征多项式相同，但不相似？",
     "options": [
         {"id": "a", "text": "I 与 [[1,1],[0,1]]"},
         {"id": "b", "text": "I 与 I"},
         {"id": "c", "text": "diag(1,2) 与 diag(1,2)"},
         {"id": "d", "text": "[[0,1],[1,0]] 与 diag(1,-1)"},
     ],
     "answer": "a", "skill": "特征值",
     "explain": "二者特征多项式都是 (λ-1)²，但后者不可对角化，与 I 不相似。",
     "ai_hint": "特征多项式相同 + 一个可对角化、一个不可对角化，就构成反例。"},

    # ---------------- 初等数论 ----------------
    {"subject": "number", "kind": "tf", "difficulty": 2,
     "prompt": "若 p 是素数，则 √p 是无理数。",
     "answer": True, "skill": "无理数",
     "explain": "若 √p=a/b 为既约分数，则 p b²=a²，故 p|a，进而 p|b，与既约矛盾。",
     "ai_hint": "素数的平方根都是无理数，证明用反证法。"},
    {"subject": "number", "kind": "tf", "difficulty": 2,
     "prompt": "两个奇数的平方和一定是偶数。",
     "answer": True, "skill": "奇偶性",
     "explain": "奇数平方仍是奇数，两个奇数之和为偶数。",
     "ai_hint": "只看奇偶性即可，不需要具体数值。"},
    {"subject": "number", "kind": "tf", "difficulty": 2,
     "prompt": "若 a² 是偶数，则 a 是偶数。",
     "answer": True, "skill": "奇偶性",
     "explain": "若 a 为奇数，则 a² 为奇数，与 a² 为偶数矛盾。",
     "ai_hint": "这是「素数 p 整除 a² 则 p 整除 a」的特例。"},
    {"subject": "number", "kind": "tf", "difficulty": 2,
     "prompt": "若 a|bc 且 gcd(a,b)=1，则 a|c。",
     "answer": True, "skill": "整除",
     "explain": "由 Bézout 定理，存在 x,y 使 ax+by=1；两边乘 c 得 acx+bcy=c，故 a|c。",
     "ai_hint": "互素是消去律成立的关键条件。"},
    {"subject": "number", "kind": "tf", "difficulty": 2,
     "prompt": "若 p 是素数且 p|ab，则 p|a 或 p|b。",
     "answer": True, "skill": "素数",
     "explain": "这是欧几里得引理，素数在整数环中是素元。",
     "ai_hint": "素数的定义等价于这条整除性质。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "若 n 是合数，则 n 必有不超过 √n 的素因子。",
     "answer": True, "skill": "因数分解",
     "explain": "若 n=ab 且 a,b>√n，则 ab>n，矛盾；取较小因子继续分解即得素因子。",
     "ai_hint": "试除法只需试到 √n。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "若 gcd(a,b)=1，则存在整数 x,y 使 ax+by=1。",
     "answer": True, "skill": "最大公因数",
     "explain": "这是 Bézout 恒等式，可由扩展欧几里得算法构造。",
     "ai_hint": "互素等价于 1 在 a,b 的整数线性组合中。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "若 a≡b (mod m)，则 a²≡b² (mod m)。",
     "answer": True, "skill": "同余",
     "explain": "a-b 被 m 整除，则 a²-b²=(a-b)(a+b) 也被 m 整除。",
     "ai_hint": "同余关系对乘法保持。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "若 a²≡b² (mod m)，则 a≡b (mod m)。",
     "answer": False, "skill": "同余",
     "explain": "反例：m=3，a=1，b=2；1²≡4≡2²，但 1≠2 (mod 3)。",
     "ai_hint": "平方同余不能直接开方，除非模数是素数且知道符号。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "对奇素数 p，同余方程 x²≡1 (mod p) 只有 x≡±1 两个解。",
     "answer": True, "skill": "二次剩余",
     "explain": "x²-1=(x-1)(x+1)≡0；p 是素数，故 p|x-1 或 p|x+1。",
     "ai_hint": "模素数时，乘积为 0 意味着至少一个因子为 0。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "2 是模 7 的二次剩余。",
     "answer": True, "skill": "二次剩余",
     "explain": "3²=9≡2 (mod 7)，所以 2 是二次剩余。",
     "ai_hint": "只需找到一个平方同余于 2 的数。"},
    {"subject": "number", "kind": "tf", "difficulty": 3,
     "prompt": "欧拉函数 φ(n) 对 n>2 一定是偶数。",
     "answer": True, "skill": "欧拉函数",
     "explain": "n>2 时，与 n 互素的数成对出现（a 与 n-a），故 φ(n) 为偶数。",
     "ai_hint": "配对论证：a 与 n-a 总是同时互素。"},
    {"subject": "number", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪组 (a,b,m) 满足 a²≡b² (mod m)，但 a≠b (mod m)？",
     "options": [
         {"id": "a", "text": "(1, 2, 3)"},
         {"id": "b", "text": "(1, 4, 3)"},
         {"id": "c", "text": "(2, 5, 3)"},
         {"id": "d", "text": "(0, 0, 5)"},
     ],
     "answer": "a", "skill": "同余",
     "explain": "1²=1，2²=4≡1 (mod 3)，但 1≠2 (mod 3)。",
     "ai_hint": "模 3 时 1 和 2 的平方都同余于 1。"},
    {"subject": "number", "kind": "counter", "difficulty": 3,
     "prompt": "下列哪个合数 n 不满足 n | 2^n - 2？",
     "options": [
         {"id": "a", "text": "4"},
         {"id": "b", "text": "2"},
         {"id": "c", "text": "3"},
         {"id": "d", "text": "5"},
     ],
     "answer": "a", "skill": "费马小定理",
     "explain": "2⁴-2=14，14 不能被 4 整除；2、3、5 是素数，不在「合数」范围内。",
     "ai_hint": "先确认 n 是合数，再代入计算。"},
    {"subject": "number", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪组 (a,b,c) 满足 a|bc，但 a∤b 且 a∤c？",
     "options": [
         {"id": "a", "text": "(6, 2, 3)"},
         {"id": "b", "text": "(4, 2, 3)"},
         {"id": "c", "text": "(5, 2, 3)"},
         {"id": "d", "text": "(3, 6, 2)"},
     ],
     "answer": "a", "skill": "整除",
     "explain": "6|2×3=6，但 6 不整除 2，也不整除 3。",
     "ai_hint": "把 a 拆成两个互不相同的因子，分别放到 b 和 c 里。"},

    # ---------------- 抽象代数 ----------------
    {"subject": "abstract", "kind": "tf", "difficulty": 2,
     "prompt": "每个群都有非平凡子群。",
     "answer": False, "skill": "子群",
     "explain": "反例：素数阶循环群 Z_p 只有 {0} 与自身两个子群。",
     "ai_hint": "素数阶群没有中间子群，这是拉格朗日定理的直接推论。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 2,
     "prompt": "阿贝尔群的每个子群都是正规子群。",
     "answer": True, "skill": "正规子群",
     "explain": "交换群中 gH=Hg 对任意 g,H 成立，因此每个子群都正规。",
     "ai_hint": "正规性要求共轭不变，交换群里共轭就是自身。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "两个正规子群的交集仍是正规子群。",
     "answer": True, "skill": "正规子群",
     "explain": "对 g∈G、x∈H∩K，gxg^{-1} 同时属于 H 与 K，因此属于交集。",
     "ai_hint": "正规性对交集保持。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "两个正规子群的并仍是正规子群。",
     "answer": False, "skill": "正规子群",
     "explain": "两个子群的并通常不是子群（不满足加法封闭），更谈不上正规。",
     "ai_hint": "子群对并运算不封闭，这是常见误区。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 2,
     "prompt": "群同态的核是正规子群。",
     "answer": True, "skill": "同态",
     "explain": "若 φ(g)=e，则 φ(xgx^{-1})=φ(x)eφ(x)^{-1}=e，故核在共轭下不变。",
     "ai_hint": "核一定是正规子群，这是第一同构定理的基础。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "群同态的像一定是陪域的正规子群。",
     "answer": False, "skill": "同态",
     "explain": "反例：S_3 的一个 2 阶子群到 S_3 的包含映射，其像不是 S_3 的正规子群。",
     "ai_hint": "像在陪域里只是子群，不一定正规；它在像自身中当然正规。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "指数为 2 的子群一定正规。",
     "answer": True, "skill": "正规子群",
     "explain": "指数为 2 时只有两个左陪集，左陪集与右陪集相同，因此正规。",
     "ai_hint": "指数 2 是非常强的条件，直接推出正规。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "S_n 的中心在 n≥3 时是平凡的。",
     "answer": True, "skill": "群中心",
     "explain": "n≥3 时，任何非恒等置换都会移动某个元素，可构造另一个置换与之不交换。",
     "ai_hint": "对称群的中心只有恒等元。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 2,
     "prompt": "每个置换都可以写成对换的乘积。",
     "answer": True, "skill": "置换",
     "explain": "任意轮换 (a1 a2 … ak) 可写成 (a1 ak)…(a1 a2)，而任意置换可分解为轮换。",
     "ai_hint": "对换是置换群的基本生成元。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 2,
     "prompt": "对换的乘积一定是偶置换。",
     "answer": False, "skill": "置换",
     "explain": "单个对换就是奇置换，它本身就是「对换的乘积」。",
     "ai_hint": "奇偶性看对换的个数，单个对换是奇数个。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 2,
     "prompt": "Z_n 的每个子群都是循环群。",
     "answer": True, "skill": "循环群",
     "explain": "循环群的子群仍循环；Z_n 的子群由某个 d|n 生成，形如 dZ_n。",
     "ai_hint": "循环群的子群保持循环性。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "两个循环群的直积一定循环。",
     "answer": False, "skill": "循环群",
     "explain": "反例：Z_2×Z_2 不是循环群，因为不存在 4 阶元素。",
     "ai_hint": "直积循环需要两个群的阶互素。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "有限整环一定是域。",
     "answer": True, "skill": "整环",
     "explain": "有限整环中非零元乘以整个环的映射是单射，从而是满射，故每个非零元可逆。",
     "ai_hint": "有限性把「无零因子」升级成了「可逆」。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "整环的子环一定是整环。",
     "answer": True, "skill": "整环",
     "explain": "子环继承加法与乘法，且整环没有零因子，子环自然也没有。",
     "ai_hint": "无零因子是向下遗传的性质。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 3,
     "prompt": "理想的并一定是理想。",
     "answer": False, "skill": "理想",
     "explain": "两个理想的并通常对加法不封闭；例如 2Z ∪ 3Z 不包含 2+3=5。",
     "ai_hint": "理想对加法要求封闭，并集通常做不到。"},
    {"subject": "abstract", "kind": "tf", "difficulty": 4,
     "prompt": "素理想一定是极大理想。",
     "answer": False, "skill": "理想",
     "explain": "反例：Z[x] 中的 (x) 是素理想，但不是极大理想，因为 (x)⊂(x,2)⊂Z[x]。",
     "ai_hint": "只有在 PID 中，非零素理想才一定极大。"},
    {"subject": "abstract", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个群没有非平凡子群？",
     "options": [
         {"id": "a", "text": "Z_5"},
         {"id": "b", "text": "Z_6"},
         {"id": "c", "text": "Z_4"},
         {"id": "d", "text": "S_3"},
     ],
     "answer": "a", "skill": "子群",
     "explain": "Z_5 是素数阶循环群，只有 {0} 与自身两个子群。",
     "ai_hint": "素数阶群一定没有中间子群。"},
    {"subject": "abstract", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪组循环群的直积不是循环群？",
     "options": [
         {"id": "a", "text": "Z_2 × Z_2"},
         {"id": "b", "text": "Z_2 × Z_3"},
         {"id": "c", "text": "Z_3 × Z_5"},
         {"id": "d", "text": "Z_5 × Z_7"},
     ],
     "answer": "a", "skill": "循环群",
     "explain": "Z_2×Z_2 的每个非零元阶都是 2，没有 4 阶元，因此不循环。",
     "ai_hint": "两个 2 阶循环群的直积不是循环群。"},
    {"subject": "abstract", "kind": "counter", "difficulty": 3,
     "prompt": "下列哪个映射的像不是陪域的正规子群？",
     "options": [
         {"id": "a", "text": "S_3 的一个 2 阶子群到 S_3 的包含映射"},
         {"id": "b", "text": "A_3 到 S_3 的包含映射"},
         {"id": "c", "text": "det: GL_2(R) → R*"},
         {"id": "d", "text": "sgn: S_3 → {±1}"},
     ],
     "answer": "a", "skill": "同态",
     "explain": "S_3 的 2 阶子群不是正规子群，因此其包含映射的像不正规。",
     "ai_hint": "包含映射的像就是子群本身，找非正规子群即可。"},
    {"subject": "abstract", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个置换是奇置换？",
     "options": [
         {"id": "a", "text": "(1 2)"},
         {"id": "b", "text": "(1 2)(3 4)"},
         {"id": "c", "text": "(1 2 3)"},
         {"id": "d", "text": "恒等置换"},
     ],
     "answer": "a", "skill": "置换",
     "explain": "(1 2) 是一个对换，属于奇置换；(1 2)(3 4) 与 (1 2 3) 都是偶置换。",
     "ai_hint": "对换个数为奇数就是奇置换。"},
    # ---------------- 离散数学 ----------------
    {"subject": "discrete", "kind": "tf", "difficulty": 2,
     "prompt": "集合 A 上的等价关系把 A 划分成互不相交的等价类。",
     "answer": True, "skill": "等价关系",
     "explain": "自反、对称、传递保证每个元素属于唯一等价类，等价类两两不交且并为 A。",
     "ai_hint": "等价关系 ↔ 划分，这是同一个东西的两种说法。"},
    {"subject": "discrete", "kind": "tf", "difficulty": 2,
     "prompt": "集合 A 上的任意二元关系都是等价关系。",
     "answer": False, "skill": "等价关系",
     "explain": "反例：整数上的 < 不自反（a<a 不成立），因此不是等价关系。",
     "ai_hint": "等价关系要同时满足自反、对称、传递三条。"},
    {"subject": "discrete", "kind": "tf", "difficulty": 3,
     "prompt": "若 R 是集合 A 上的偏序关系，则 A 中任意两个元素都可以比较。",
     "answer": False, "skill": "集合与关系",
     "explain": "偏序只要求自反、反对称、传递；全序才要求任意两元素可比。反例：集合包含关系下 {1} 与 {2} 不可比。",
     "ai_hint": "偏序 ≠ 全序，哈斯图上有不可比元素很常见。"},
    {"subject": "discrete", "kind": "tf", "difficulty": 3,
     "prompt": "若 |A|=n，则 A 上共有 2^(n²) 个二元关系。",
     "answer": True, "skill": "集合与关系",
     "explain": "二元关系是 A×A 的子集，而 |A×A|=n²，子集数为 2^(n²)。",
     "ai_hint": "关系就是笛卡尔积的子集，数子集即可。"},
    {"subject": "discrete", "kind": "tf", "difficulty": 2,
     "prompt": "树是无圈连通图。",
     "answer": True, "skill": "树",
     "explain": "这是树的等价定义之一：连通且无圈；n 个顶点的树恰有 n-1 条边。",
     "ai_hint": "无圈保证没有多余路径，连通保证整体是一块。"},
    {"subject": "discrete", "kind": "tf", "difficulty": 3,
     "prompt": "连通图 G 恰有两个奇度顶点，则 G 没有欧拉回路，但存在欧拉路径。",
     "answer": True, "skill": "欧拉图",
     "explain": "欧拉回路要求所有顶点度数为偶数；恰有两个奇度顶点时可以一笔画，起终点为这两个奇度点。",
     "ai_hint": "0 个奇度点 ↔ 欧拉回路；2 个奇度点 ↔ 欧拉路径。"},
    {"subject": "discrete", "kind": "tf", "difficulty": 3,
     "prompt": "非平凡连通图是欧拉图，当且仅当它的每个顶点度数都是偶数。",
     "answer": True, "skill": "欧拉图",
     "explain": "欧拉定理：连通图存在经过每条边恰好一次的闭迹，等价于所有顶点度数为偶数。",
     "ai_hint": "每次进入一个顶点就要从它离开，度数必须成对消耗。"},
    {"subject": "discrete", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个是整数集上的等价关系？",
     "options": [
         {"id": "a", "text": "a ≡ b (mod 3)"},
         {"id": "b", "text": "a < b"},
         {"id": "c", "text": "a + b 为奇数"},
         {"id": "d", "text": "a 是 b 的倍数"},
     ],
     "answer": "a", "skill": "等价关系",
     "explain": "同余模 3 满足自反、对称、传递；< 不自反，a+b 为奇数不自反，倍数关系不对称。",
     "ai_hint": "逐个检查自反、对称、传递，先看自反最省事。"},
    {"subject": "discrete", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个图不是树？",
     "options": [
         {"id": "a", "text": "三角形 K_3"},
         {"id": "b", "text": "一条边 K_2"},
         {"id": "c", "text": "单顶点图"},
         {"id": "d", "text": "4 个顶点的路径 P_4"},
     ],
     "answer": "a", "skill": "树",
     "explain": "K_3 有圈，删掉任一条边仍连通，不满足树的无圈要求。",
     "ai_hint": "树不能有圈；三角形是最小的圈。"},
    {"subject": "discrete", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个图存在欧拉回路？",
     "options": [
         {"id": "a", "text": "K_5"},
         {"id": "b", "text": "K_4"},
         {"id": "c", "text": "路径 P_3"},
         {"id": "d", "text": "星形图 K_{1,3}"},
     ],
     "answer": "a", "skill": "欧拉图",
     "explain": "K_5 每个顶点度为 4（偶数）且连通，存在欧拉回路；K_4 度为 3，P_3 与 K_{1,3} 都有奇度顶点。",
     "ai_hint": "先数每个顶点的度数，全偶才可能有欧拉回路。"},
    {"subject": "discrete", "kind": "counter", "difficulty": 2,
     "prompt": "下列哪个命题公式是重言式？",
     "options": [
         {"id": "a", "text": "p ∨ ¬p"},
         {"id": "b", "text": "p ∧ ¬p"},
         {"id": "c", "text": "p → ¬p"},
         {"id": "d", "text": "p ↔ ¬p"},
     ],
     "answer": "a", "skill": "命题逻辑",
     "explain": "排中律 p∨¬p 恒真；p∧¬p 恒假，p→¬p 在 p 真时为假，p↔¬p 恒假。",
     "ai_hint": "重言式对所有赋值都为真，代 p=真、p=假各试一次。"},
]

# ------------------------------------------------------------
# 新增课程的题库元信息：提示 / 考试套路 / 应用场景
# ------------------------------------------------------------

SUBJECT_HINTS = {
    "analysis": {
        "tf": ["先分清条件与结论的方向：是「谁推谁」。", "尝试用尖点、振荡数列、调和级数这类最小反例。"],
        "counter": ["逐项检查是否真的满足题干前提。", "把候选代入定义，重点看边界与退化情形。"],
    },
    "algebra": {
        "tf": ["先确认命题对加法、乘法或相似变换是否保持。", "用 2×2 矩阵、对角矩阵或幂零矩阵构造最小反例。"],
        "counter": ["先排除明显不满足前提的选项。", "把候选代入矩阵运算或特征多项式验证。"],
    },
    "number": {
        "tf": ["先看整除、互素、同余各自的条件。", "用最小合数或小模数代入检验。"],
        "counter": ["逐项检查整除/同余关系是否成立。", "注意「合数」「互素」「模数」这些前提。"],
    },
    "abstract": {
        "tf": ["先回到子群、正规子群、同态核的定义。", "用低阶群（Z_4、Z_2×Z_2、S_3）构造反例。"],
        "counter": ["先确认选项里的对象是否构成群/子群。", "用拉格朗日定理或元素阶排除选项。"],
    },
    "probability": {
        "tf": ["先写出事件及概率公式，区分条件概率与联合概率。", "检查是否用到独立性、全概率或 Bayes 公式。"],
        "counter": ["先逐项检查事件关系是否符合题干。", "把候选代入 P(A∩B)=P(A)P(B) 或条件概率公式。"],
    },
    "statistics": {
        "tf": ["先分清总体参数与样本统计量。", "回到无偏性、置信区间或假设检验的定义。"],
        "counter": ["先看统计量的定义与适用条件。", "用无偏性、标准误或检验规则逐项排除。"],
    },
    "ode": {
        "tf": ["先判断方程类型：可分离、一阶线性还是常系数。", "把候选解代回方程与初值条件检验。"],
        "counter": ["先看方程阶数与线性/非线性。", "把候选函数求导后代入原方程。"],
    },
    "complex": {
        "tf": ["先看解析性、Cauchy–Riemann 方程与奇点类型。", "注意辐角的多值性和积分方向。"],
        "counter": ["先判断函数在指定点附近是否可导。", "用 CR 方程或留数公式逐项验证。"],
    },
    "geometry": {
        "tf": ["先画图，写出向量、平面或直线方程。", "用点积、叉积或距离公式验证。"],
        "counter": ["先写出每个选项对应的方程或向量。", "代入垂直、平行、过点等条件检查。"],
    },
    "numerical": {
        "tf": ["先判断用的是哪个公式及其误差阶。", "注意收敛条件、迭代次数和有效数字。"],
        "counter": ["先看方法是否需要导数、是否保证收敛。", "比较代数精度或误差阶排除选项。"],
    },
    "modeling": {
        "tf": ["先写清假设、变量和单位。", "把文字条件翻译成方程，再检查结果是否合理。"],
        "counter": ["先明确题目问的是模型选择还是指标含义。", "用假设、量纲和适用条件逐项排除。"],
    },
}

EXAM_TIPS = {
    "analysis": "极限/积分题先写存在条件再算；端点、符号和取整是常见丢分点。",
    "algebra": "先看秩与特征值，再决定用消元、对角化还是相似不变量。",
    "number": "先分解质因数或看模数，再用同余性质；注意 gcd=1 的前提。",
    "abstract": "先找子群/正规性/同态核的定义，再用最小反例。",
    "probability": "先画事件树或列联合概率表，再套全概率/Bayes；注意条件概率的分母。",
    "statistics": "先写清 H0/H1 与统计量，再查分布；区间估计注意标准误。",
    "ode": "先分类再套通解，最后用初值定常数并代回检验。",
    "complex": "先判解析性，再算留数；围道积分注意方向和奇点是否在内部。",
    "geometry": "先建坐标系/写方程，再用点积叉积；法向量不唯一。",
    "numerical": "先看误差阶和收敛条件，再代数；迭代题注意停止准则。",
    "modeling": "先写假设与单位，再建模；最后做量纲和合理性检验。",
}

# 已有计算题的提示按「知识点」给，避免所有题都退化成同一句空话
SKILL_HINTS = {
    "最大公因数": ["用辗转相除：用较小数除较大数，余数继续。", "最后一次非零余数就是最大公因数。"],
    "欧拉函数": ["先把 n 分解成素数幂。", "φ(n)=n∏(1-1/p)，逐个素因子相乘。"],
    "模逆": ["解 ax+my=1，用扩展欧几里得。", "把 x 取模 m，得到 0..m-1 的逆元。"],
    "模幂": ["把指数拆成二进制，逐次平方取模。", "每一步只保留 mod m 的余数。"],
    "因数个数": ["先把 n 写成 ∏p^e。", "因数个数=∏(e+1)。"],
    "中国剩余定理": ["两个模数互素，可以分别处理。", "在 0..mn-1 内找同时满足两式的最小解。"],
    "二次剩余": ["用欧拉判别法算 a^((p-1)/2) mod p。", "1 表示剩余，-1 表示非剩余，0 表示整除。"],
    "行列式": ["二阶用主对角积减副对角积。", "三阶按一行展开，注意代数余子式符号。"],
    "矩阵的秩": ["做初等行变换，化成阶梯形。", "非零行的数量就是秩。"],
    "特征值": ["解 det(λI-A)=0。", "二阶可用 λ²-tr(A)λ+det(A)=0。"],
    "矩阵的迹": ["迹等于主对角线元素之和。", "不要把非对角元加进去。"],
    "线性方程组": ["先看系数行列式是否非零。", "用消元或克拉默法则求 x。"],
    "零空间": ["零度 = 未知数个数 - 秩。", "先求矩阵的秩。"],
    "极限": ["先判断是 0/0、∞/∞ 还是重要极限。", "用等价无穷小或最高次项系数。"],
    "导数": ["幂函数求导：x^n → n x^(n-1)。", "先求导函数，再代入 x 值。"],
    "定积分": ["先找原函数。", "代入上下限相减，注意符号。"],
    "级数": ["等比级数求和 1/(1-r)，要求 |r|<1。", "先确认公比 r。"],
    "泰勒展开": ["e^x 的系数是 1/n!。", "不要漏掉阶乘。"],
    "元素阶": ["在 Z_n 中，元素 a 的阶 = n/gcd(n,a)。", "先算 gcd。"],
    "子群": ["循环群的子群与 n 的正因数一一对应。", "先数 n 的因数个数。"],
    "同态核": ["核的大小 = gcd(n,m)。", "先算两个阶的最大公因数。"],
    "对称群": ["|S_n|=n!。", "从 n 乘到 1。"],
    "初等交换群": ["除零元外每个元素的阶都是 2。", "总数 2^k 减去零元。"],
}

# 给老题库补上「用在哪」，让刷题不只停在符号层面
SCENE_BY_SKILL = {
    "最大公因数": "分数约分、密码学中的模运算",
    "欧拉函数": "RSA 加密与互素计数",
    "模逆": "密码学与同余方程求解",
    "模幂": "RSA、哈希与快速幂",
    "因数个数": "整数分解、约数枚举",
    "中国剩余定理": "周期排班、历法与编码",
    "二次剩余": "密码学与数论算法",
    "行列式": "图像变换、线性方程组",
    "矩阵的秩": "数据降维、方程组可解性",
    "特征值": "振动分析、PCA、稳定性",
    "矩阵的迹": "相似不变量与特征值之和",
    "线性方程组": "电路、配平与经济均衡",
    "零空间": "数据压缩与解空间",
    "极限": "瞬时速度、连续复利",
    "导数": "边际成本、速度与最优化",
    "定积分": "面积、累积量与概率",
    "级数": "复利、信号展开",
    "泰勒展开": "近似计算与误差估计",
    "元素阶": "循环群与密码学",
    "子群": "对称性与结构分解",
    "同态核": "信息损失与编码",
    "对称群": "排列组合与魔方",
    "初等交换群": "二进制向量空间",
}


def _split_steps(text):
    parts = [p.strip(" \t\r\n。；;.") for p in re.split(r"[。；;\n]", text or "")]
    return [p for p in parts if p] or ([text] if text else [])


def _default_hints(q):
    kind = q.get("kind", "calc")
    if kind == "calc":
        return SKILL_HINTS.get(q.get("skill")) or [
            "先确定这题考的定义/公式，再整理已知量。",
            "代入时注意符号、单位和取整要求。"]
    table = SUBJECT_HINTS.get(q.get("subject"), {})
    return table.get(kind) or ["先回到相关定义，逐项检查条件。",
                               "尝试用最小反例或边界情形检验。"]


def _course_of(subject, skill):
    """把大学科 + 知识点映射到选课粒度；数学分析/高等代数按学期拆分。"""
    if subject == "analysis":
        if skill in ("数列极限", "连续与可导", "导数应用", "极限", "导数"):
            return "analysis1"
        if skill in ("级数", "黎曼积分", "定积分", "泰勒展开"):
            return "analysis2"
        return "analysis3"
    if subject == "algebra":
        if skill in ("矩阵运算", "行列式", "线性方程组", "矩阵的秩",
                     "零空间", "多项式"):
            return "algebra1"
        return "algebra2"
    return subject if subject in COURSE_NAMES else None


def _decorate(q):
    """补齐提示、步骤、考试套路与应用场景；题库和生成器都可以只写核心字段。"""
    q["course"] = q.get("course") or _course_of(q.get("subject"), q.get("skill", ""))
    q["hints"] = list(q.get("hints") or _default_hints(q))[:2]
    q["steps"] = list(q.get("steps") or _split_steps(q.get("explain", "")))[:6]
    q["exam_tip"] = q.get("exam_tip") or EXAM_TIPS.get(q.get("subject"), "")
    q["scene"] = q.get("scene") or SCENE_BY_SKILL.get(q.get("skill"), "")
    q["ai_hint"] = q.get("ai_hint") or q.get("explain", "")
    return q


def _c_tf(subject, prompt, answer, skill, explain, difficulty=2,
          hints=None, exam_tip="", scene="", ai_hint=None):
    return {
        "subject": subject, "kind": "tf", "difficulty": difficulty,
        "prompt": prompt, "answer": bool(answer), "skill": skill,
        "explain": explain, "ai_hint": ai_hint or explain,
        "hints": hints or [], "exam_tip": exam_tip, "scene": scene,
    }


def _c_counter(subject, prompt, options, answer, skill, explain, difficulty=2,
               hints=None, exam_tip="", scene="", ai_hint=None):
    return {
        "subject": subject, "kind": "counter", "difficulty": difficulty,
        "prompt": prompt, "skill": skill, "explain": explain,
        "ai_hint": ai_hint or explain, "hints": hints or [],
        "exam_tip": exam_tip, "scene": scene,
        "options": [{"id": i, "text": t} for i, t in options],
        "answer": answer,
    }


# ------------------------------------------------------------
# 概率论 / 数理统计（人工校验）
# ------------------------------------------------------------

CONCEPTS += [
    _c_tf("probability", "若 P(A)>0 且 P(B|A)=P(B)，则 A 与 B 相互独立。", True,
          "条件概率与独立性",
          "由 P(B|A)=P(A∩B)/P(A)=P(B)，两边乘 P(A) 得 P(A∩B)=P(A)P(B)。",
          difficulty=2, scene="抽奖、质检中的独立重复试验"),
    _c_tf("probability", "互斥事件一定相互独立。", False,
          "互斥与独立",
          "互斥时 P(A∩B)=0；只有 P(A) 或 P(B) 为 0 时才等于 P(A)P(B)，否则不独立。",
          difficulty=2,
          hints=["独立要求 P(A∩B)=P(A)P(B)。", "互斥时 P(A∩B) 等于多少？"]),
    _c_tf("probability", "若 A 与 B 独立，则 A 与 B 的对立事件也独立。", True,
          "独立性",
          "P(A∩B^c)=P(A)-P(A∩B)=P(A)-P(A)P(B)=P(A)(1-P(B))。",
          difficulty=3),
    _c_tf("probability", "若随机变量 X 的方差存在，则 E(X) 一定存在。", True,
          "期望与方差",
          "方差有限说明 E(X²) 有限；由 Cauchy–Schwarz，E|X|≤sqrt(E(X²))，故 E(X) 存在。",
          difficulty=3, scene="风险度量中先确认期望是否存在"),
    _c_tf("probability", "若 X 与 Y 不相关，则 X 与 Y 独立。", False,
          "相关与独立",
          "不相关只说明 Cov(X,Y)=0；例如 X~U(-1,1)、Y=X²，E(XY)=0，但 Y 由 X 决定。",
          difficulty=3,
          hints=["不相关只排除线性相关。", "找一个 Y 由 X 决定但协方差为 0 的例子。"]),
    _c_counter("probability", "下列哪组事件互斥但不独立？",
               [("a", "掷骰子：A={1}，B={2}"),
                ("b", "两次独立掷硬币：A=第一次正面，B=第二次正面"),
                ("c", "掷骰子：A=偶数，B={6}"),
                ("d", "掷骰子：A={1}，B=奇数")], "a",
               "互斥与独立",
               "A、B 不能同时发生，P(A∩B)=0；但 P(A)P(B)=1/36≠0，所以不独立。",
               difficulty=2, scene="抽签与中奖概率"),
    _c_counter("probability", "下列哪个随机变量是离散型？",
               [("a", "二项分布 B(n,p)"), ("b", "正态分布 N(μ,σ²)"),
                ("c", "均匀分布 U(0,1)"), ("d", "指数分布 Exp(λ)")], "a",
               "随机变量类型",
               "二项分布只取 0,1,…,n 这些离散值；其余三种都是连续型。",
               difficulty=1),
    _c_counter("probability", "下列哪项成立时，P(A|B)=P(A)（P(B)>0）？",
               [("a", "A 与 B 独立"), ("b", "A 与 B 互斥"),
                ("c", "A 是 B 的子事件"), ("d", "B 是 A 的子事件")], "a",
               "条件概率",
               "独立时 P(A|B)=P(A∩B)/P(B)=P(A)P(B)/P(B)=P(A)。",
               difficulty=2),

    _c_tf("statistics", "样本均值是总体均值的无偏估计。", True,
          "无偏估计",
          "E(X̄)=E((X1+…+Xn)/n)=nμ/n=μ，因此样本均值无偏。",
          difficulty=2, scene="用样本推断总体平均收入"),
    _c_tf("statistics", "样本方差 S²=(1/n)Σ(Xi-X̄)² 是总体方差的无偏估计。", False,
          "无偏估计",
          "分母用 n 的样本方差是有偏的；无偏估计应除以 n-1（贝塞尔校正）。",
          difficulty=3,
          hints=["无偏性看 E(S²) 是否等于 σ²。", "想一想自由度为什么少了一个。"]),
    _c_tf("statistics", "95% 置信区间的含义是「真实参数以 95% 的概率落在该区间内」。", False,
          "置信区间",
          "频率派解释：重复抽样构造区间，约 95% 的区间会覆盖真实参数；参数本身不是随机变量。",
          difficulty=3, scene="A/B 测试与民意调查"),
    _c_tf("statistics", "假设检验中，p 值小于显著性水平 α 时拒绝原假设。", True,
          "假设检验",
          "p 值是在 H0 成立时出现当前或更极端结果的概率；p<α 时拒绝 H0。",
          difficulty=2),
    _c_tf("statistics", "p 值是原假设为真的概率。", False,
          "假设检验",
          "p 值不是 P(H0 为真)，而是在 H0 下观察到当前数据的概率。",
          difficulty=3,
          hints=["p 值的条件概率方向是什么？", "它是「H0 成立时数据有多极端」，不是「H0 有多真」。"]),
    _c_counter("statistics", "下列哪个是总体方差的无偏估计？",
               [("a", "1/(n-1) Σ(Xi-X̄)²"), ("b", "1/n Σ(Xi-X̄)²"),
                ("c", "样本极差"), ("d", "样本中位数")], "a",
               "无偏估计",
               "除以 n-1 的样本方差满足 E(S²)=σ²；除以 n 的版本有偏。",
               difficulty=2),
    _c_counter("statistics", "其他条件不变，下列哪项会缩短置信区间？",
               [("a", "增大样本量"), ("b", "提高置信水平"),
                ("c", "增大总体方差"), ("d", "减小样本量")], "a",
               "置信区间",
               "区间半宽与 σ/√n 成正比；样本量增大，标准误变小，区间变窄。",
               difficulty=2, scene="实验设计：多采样能提高估计精度"),
    _c_counter("statistics", "下列哪项是极大似然估计的特点？",
               [("a", "使观测数据出现的似然函数最大"), ("b", "一定无偏"),
                ("c", "一定是最小方差估计"), ("d", "只适用于正态分布")], "a",
               "极大似然",
               "MLE 的定义就是最大化似然函数；它不一定无偏，也不一定最小方差。",
               difficulty=2),
]

# ------------------------------------------------------------
# 常微分方程 / 复变函数 / 解析几何（人工校验）
# ------------------------------------------------------------

CONCEPTS += [
    _c_tf("ode", "初值问题 y'=y，y(0)=0 的唯一解是 y=0。", True,
          "初值问题",
          "y=0 满足方程与初值；由解的存在唯一性（或分离变量）可知它是唯一解。",
          difficulty=2, scene="无初始扰动时系统保持在平衡态"),
    _c_tf("ode", "一阶线性方程 y'+P(x)y=Q(x) 可用积分因子 e^{∫P(x)dx} 求解。", True,
          "一阶线性方程",
          "两边乘积分因子后，左边化为 (e^{∫P dx} y)'，再积分即可。",
          difficulty=2),
    _c_tf("ode", "二阶线性齐次方程的两个解之和仍是解。", True,
          "线性方程",
          "若 L[y1]=0、L[y2]=0，则 L[y1+y2]=L[y1]+L[y2]=0。",
          difficulty=2),
    _c_tf("ode", "若 y1、y2 线性相关，则它们的 Wronskian 恒为 0。", True,
          "Wronskian",
          "线性相关时 y2=Cy1，W=y1y2'-y1'y2=y1(Cy1')-y1'(Cy1)=0。",
          difficulty=3),
    _c_tf("ode", "方程 y''+y=0 的通解是 C1 cos x + C2 sin x。", True,
          "二阶常系数方程",
          "特征方程 r²+1=0 的根为 ±i，因此通解为 C1 cos x+C2 sin x。",
          difficulty=2),
    _c_counter("ode", "下列哪个是 y'=2y 的解？",
               [("a", "y=e^{2x}"), ("b", "y=e^{-2x}"),
                ("c", "y=2x"), ("d", "y=x²")], "a",
               "一阶方程",
               "对 y=e^{2x} 求导得 y'=2e^{2x}=2y。",
               difficulty=1),
    _c_counter("ode", "下列哪组函数在 R 上线性无关？",
               [("a", "{e^x, e^{2x}}"), ("b", "{x, 2x}"),
                ("c", "{1, sin²x+cos²x}"), ("d", "{e^x, 2e^x}")], "a",
               "线性相关与无关",
               "e^x 与 e^{2x} 的比值不是常数，Wronskian 非零；其余各组都成比例。",
               difficulty=2),
    _c_counter("ode", "下列哪个方程是一阶线性方程？",
               [("a", "y'+2xy=x"), ("b", "y'=y²"),
                ("c", "y''+y=0"), ("d", "y'=sin y")], "a",
               "方程分类",
               "y'+2xy=x 对 y 及其导数都是一次的；其余分别是一阶非线性、二阶、非线性。",
               difficulty=2),

    _c_tf("complex", "对任意复数 z1、z2，有 |z1 z2|=|z1||z2|。", True,
          "复数的模",
          "若 z1=r1e^{iθ1}、z2=r2e^{iθ2}，则 z1z2=r1r2e^{i(θ1+θ2)}，模相乘。",
          difficulty=1, scene="信号处理中的幅度相乘"),
    _c_tf("complex", "arg(z1z2)=arg z1+arg z2（mod 2π）。", True,
          "复数的辐角",
          "由指数形式相乘直接得到；注意辐角要按模 2π 理解。",
          difficulty=2),
    _c_tf("complex", "解析函数一定满足 Cauchy–Riemann 方程。", True,
          "Cauchy–Riemann 方程",
          "解析意味着复可导，比较实部与虚部的偏导即得 u_x=v_y、u_y=-v_x。",
          difficulty=2),
    _c_tf("complex", "若 u、v 满足 Cauchy–Riemann 方程，则 f=u+iv 一定解析。", False,
          "解析性判定",
          "还需要偏导数连续（或 f 可微）；仅逐点满足 CR 不足以保证在邻域内解析。",
          difficulty=4,
          hints=["CR 方程是必要条件还是充分条件？", "想一想 |z|² 在 0 点满足 CR 但只在一点可导。"]),
    _c_tf("complex", "e^z 在复平面上处处解析。", True,
          "初等解析函数",
          "e^z=e^x(cos y+i sin y)，实虚部偏导连续且满足 CR 方程。",
          difficulty=2),
    _c_counter("complex", "下列哪个函数在 z=0 处解析？",
               [("a", "e^z"), ("b", "1/z"), ("c", "\\bar z"), ("d", "|z|²")], "a",
               "解析函数",
               "e^z 处处解析；1/z 在 0 无定义，\\bar z 不满足 CR，|z|² 只在 0 点满足 CR 而不在邻域解析。",
               difficulty=2),
    _c_counter("complex", "沿逆时针单位圆积分，下列哪个等于 2πi？",
               [("a", "∮ 1/z dz"), ("b", "∮ 1/z² dz"),
                ("c", "∮ z dz"), ("d", "∮ \\bar z dz")], "a",
               "围道积分",
               "1/z 在单位圆内有一个一阶极点，留数为 1，积分 2πi·Res=2πi；1/z² 的留数为 0。",
               difficulty=3),
    _c_counter("complex", "下列哪个点是 1/(z²+1) 的奇点？",
               [("a", "z=i"), ("b", "z=0"), ("c", "z=1"), ("d", "z=-1")], "a",
               "奇点",
               "z²+1=(z-i)(z+i)，奇点为 ±i；选项中只有 i。",
               difficulty=2),

    _c_tf("geometry", "两个非零向量点积为 0，则它们垂直。", True,
          "向量点积",
          "u·v=|u||v|cos θ=0，且 |u|、|v| 非零，所以 cos θ=0，θ=90°。",
          difficulty=1),
    _c_tf("geometry", "两个非零向量叉积为 0，则它们平行。", True,
          "向量叉积",
          "|u×v|=|u||v|sin θ=0，非零向量只能 sin θ=0，即 θ=0 或 π。",
          difficulty=2),
    _c_tf("geometry", "平面的法向量是唯一的。", False,
          "平面方程",
          "法向量的任意非零倍数仍是法向量；平面方程也不唯一。",
          difficulty=2),
    _c_tf("geometry", "点到平面的距离公式中，分母是法向量的模。", True,
          "点到平面距离",
          "距离 d=|ax0+by0+cz0+d|/sqrt(a²+b²+c²)，分母正是法向量 (a,b,c) 的模。",
          difficulty=2),
    _c_tf("geometry", "旋转不改变向量长度。", True,
          "旋转变换",
          "旋转矩阵是正交矩阵，保持内积与长度。",
          difficulty=2, scene="机器人姿态变换、图形旋转"),
    _c_counter("geometry", "下列哪组向量垂直？",
               [("a", "(1,2,3) 与 (1,1,-1)"), ("b", "(1,0,0) 与 (1,1,1)"),
                ("c", "(0,1,0) 与 (1,1,1)"), ("d", "(0,0,1) 与 (1,1,1)")], "a",
               "向量垂直",
               "(1,2,3)·(1,1,-1)=1+2-3=0；其余点积分别为 1、1、1。",
               difficulty=2),
    _c_counter("geometry", "下列哪个平面过原点？",
               [("a", "2x+3y-z=0"), ("b", "2x+3y-z=1"),
                ("c", "x=1"), ("d", "z=2")], "a",
               "平面方程",
               "把 (0,0,0) 代入，只有 2x+3y-z=0 成立。",
               difficulty=1),
    _c_counter("geometry", "下列哪个方程表示球面？",
               [("a", "x²+y²+z²=4"), ("b", "x²+y²-z²=1"),
                ("c", "x+y+z=1"), ("d", "xy=1")], "a",
               "二次曲面",
               "x²+y²+z²=4 是球心在原点、半径为 2 的球面；b 是双曲面，c 是平面，d 是双曲柱面。",
               difficulty=2),
]

# ------------------------------------------------------------
# 数值分析 / 数学建模（人工校验）
# ------------------------------------------------------------

CONCEPTS += [
    _c_tf("numerical", "绝对误差有量纲，相对误差无量纲。", True,
          "误差",
          "绝对误差带被测量量的单位；相对误差是绝对误差与真值之比，通常用百分数表示。",
          difficulty=2),
    _c_tf("numerical", "若函数在区间上连续且两端异号，二分法一定收敛，但通常是线性收敛。", True,
          "二分法",
          "每次把区间减半，误差上限随之减半；只要端点异号且函数连续，就必收敛。",
          difficulty=2),
    _c_tf("numerical", "Newton 法在单根附近通常具有二次收敛。", True,
          "Newton 法",
          "在单根附近，误差满足 e_{n+1}≈(f''/2f')e_n²，所以是二次收敛。",
          difficulty=3),
    _c_tf("numerical", "Newton 法对任意初值都收敛。", False,
          "Newton 法",
          "初值离根太远、导数接近 0 或函数振荡时可能发散或跳出定义域。",
          difficulty=2, scene="工程迭代求解需要选合理初值"),
    _c_tf("numerical", "Simpson 公式对三次多项式精确。", True,
          "数值积分",
          "Simpson 公式的代数精度为 3，因此对次数不超过 3 的多项式积分精确。",
          difficulty=3),
    _c_counter("numerical", "下列哪个方法不需要计算导数？",
               [("a", "二分法"), ("b", "Newton 法"),
                ("c", "牛顿下山法"), ("d", "阻尼 Newton 法")], "a",
               "求根方法",
               "二分法只用函数值与符号；其余方法都要用导数。",
               difficulty=2),
    _c_counter("numerical", "下列哪个求积公式的代数精度最高？",
               [("a", "Simpson 公式"), ("b", "左矩形公式"),
                ("c", "右矩形公式"), ("d", "梯形公式")], "a",
               "数值积分",
               "左/右矩形精度为 0，梯形为 1，Simpson 为 3。",
               difficulty=2),
    _c_counter("numerical", "下列哪项会增大线性方程组解的误差敏感性？",
               [("a", "条件数变大"), ("b", "条件数变小"),
                ("c", "增加有效数字"), ("d", "使用更高精度浮点")], "a",
               "条件数",
               "条件数衡量输入扰动被放大的倍数；条件数越大，解越敏感。",
               difficulty=3),

    _c_tf("modeling", "即使检测的灵敏度和特异度都很高，阳性后的患病概率仍可能很低。", True,
          "Bayes 与假阳性",
          "当患病率很低时，大量健康人会产生假阳性；后验概率可能远低于直觉。",
          difficulty=3, scene="医学检测、欺诈检测中的基率谬误"),
    _c_tf("modeling", "名义利率相同，复利频率越高，实际年利率越高。", True,
          "复利",
          "实际年利率 (1+r/m)^m-1 随 m 增大而增大，连续复利时趋于 e^r-1。",
          difficulty=2, scene="贷款、信用卡与理财产品的真实成本"),
    _c_tf("modeling", "线性规划若存在有限最优解，则一定可以在可行域顶点取得最优值。", True,
          "线性规划",
          "线性目标函数在有界多面体上的最优值出现在顶点；这是单纯形法的依据。",
          difficulty=3, scene="资源分配、排班与生产计划"),
    _c_tf("modeling", "指数增长模型适用于资源无限、增长率恒定的场景。", True,
          "指数模型",
          "指数模型假设增长率与当前数量成正比且环境容量无限；资源受限时应改用 Logistic 等模型。",
          difficulty=2),
    _c_tf("modeling", "冷却模型 T(t)=T_env+(T0-T_env)e^{-kt} 中，当 k>0 时温度最终趋于环境温度。", True,
          "冷却模型",
          "指数项 e^{-kt}→0，因此 T(t)→T_env。",
          difficulty=2, scene="食品冷却、设备散热"),
    _c_tf("modeling", "Little 定律 L=λW 中，L 是系统中的平均顾客数，W 是平均停留时间。", True,
          "排队模型",
          "Little 定律：平均队长 = 到达率 × 平均停留时间。",
          difficulty=2, scene="排队、库存与在制品管理"),
    _c_tf("modeling", "相关不等于因果。", True,
          "数据分析",
          "两个变量同时变化可能由第三个混杂因素导致；因果推断需要实验或因果模型。",
          difficulty=1, scene="A/B 测试、业务指标分析"),
    _c_tf("modeling", "模型越复杂，预测效果一定越好。", False,
          "模型选择",
          "复杂模型可能过拟合、增加方差；应在偏差与方差之间权衡，并用新数据检验。",
          difficulty=2),
    _c_counter("modeling", "下列哪个场景最适合用指数衰减模型？",
               [("a", "放射性衰变"), ("b", "复利增长"),
                ("c", "无约束人口增长"), ("d", "线性折旧")], "a",
               "模型选择",
               "放射性衰变速率与当前质量成正比且不断减少，符合指数衰减。",
               difficulty=2),
    _c_counter("modeling", "下列哪个指标最能反映贷款的真实资金成本？",
               [("a", "实际年利率"), ("b", "名义年利率"),
                ("c", "月供金额"), ("d", "贷款总额")], "a",
               "金融建模",
               "实际年利率把复利频率和费用考虑进去，便于比较不同产品。",
               difficulty=2, scene="房贷、消费贷与信用卡"),
    _c_counter("modeling", "数学建模的第一步通常是什么？",
               [("a", "明确问题、假设与变量"), ("b", "直接套公式"),
                ("c", "先写代码"), ("d", "先做实验")], "a",
               "建模流程",
               "先明确要回答的问题、可接受的假设和变量/单位，后面才有可检验的模型。",
               difficulty=1),
    _c_counter("modeling", "下列哪项属于模型检验？",
               [("a", "用新数据比较预测与观测"), ("b", "增加变量"),
                ("c", "简化模型"), ("d", "撰写论文")], "a",
               "建模流程",
               "模型检验是用未参与建模的数据评估预测误差与合理性；增加/简化变量属于建模调整。",
               difficulty=2),
    _c_counter("modeling", "下列哪个模型适合描述「增长率先增后减、最终趋于承载上限」的人口？",
               [("a", "Logistic 模型"), ("b", "指数增长模型"),
                ("c", "线性模型"), ("d", "常数模型")], "a",
               "人口模型",
               "Logistic 模型 dN/dt=rN(1-N/K) 同时包含增长与容量限制，曲线呈 S 形。",
               difficulty=2),
    _c_counter("modeling", "下列哪个指标用于衡量排队系统的服务强度？",
               [("a", "利用率 ρ=λ/μ"), ("b", "平均等待时间"),
                ("c", "队列长度"), ("d", "服务台数量")], "a",
               "排队模型",
               "ρ=λ/μ 是到达率与服务率的比值；ρ<1 时系统才稳定。",
               difficulty=2),
]

# ------------------------------------------------------------
# 数学分析（三）/ 高等代数（一、二）补充题（人工校验）
# ------------------------------------------------------------

CONCEPTS += [
    _c_tf("analysis", "若 f 在点 P 的偏导数存在，则 f 在 P 连续。", False,
          "多元连续与偏导",
          "偏导只沿坐标轴方向；反例 f(x,y)=xy/(x²+y²)（原点补 0）在原点偏导为 0，"
          "但沿 y=x 趋近时极限为 1/2，不连续。",
          difficulty=3,
          hints=["偏导只保证沿 x 轴、y 轴方向的变化。",
                 "找一个沿直线方向极限不同的二元函数。"],
          scene="多元函数的连续性与可微性判断"),
    _c_tf("analysis", "若 f 在点 P 可微，则 f 在 P 的偏导数存在。", True,
          "全微分",
          "可微的定义要求线性主部存在，沿坐标轴取增量即得两个偏导数存在。",
          difficulty=2),
    _c_tf("analysis", "若混合偏导 f_xy 与 f_yx 在点 P 连续，则二者相等。", True,
          "混合偏导",
          "这是混合偏导相等的充分条件（Clairaut/Schwarz 定理）。",
          difficulty=3,
          hints=["先回忆混合偏导相等的条件。", "连续性在这里起什么作用？"]),
    _c_tf("analysis", "f 在矩形闭区域上连续时，二重积分可以化为两个累次积分。", True,
          "重积分",
          "连续函数在矩形区域上满足 Fubini 定理，积分次序可以交换。",
          difficulty=2),
    _c_tf("analysis", "Green 公式要求闭曲线取正向，且 P、Q 在闭区域上有连续偏导。", True,
          "Green 公式",
          "正向（逆时针）对应区域在曲线左侧；缺少连续偏导时公式可能不成立。",
          difficulty=3,
          scene="平面流体的环量与旋度"),
    _c_counter("analysis", "下列哪个函数在原点偏导存在但不连续？",
               [("a", "f(x,y)=xy/(x²+y²)（原点补 0）"),
                ("b", "f(x,y)=x²+y²"), ("c", "f(x,y)=x+y"),
                ("d", "f(x,y)=sin(xy)")], "a",
               "多元连续与偏导",
               "a 在原点两个偏导都为 0，但沿 y=x 极限为 1/2，不连续；其余三个都连续。",
               difficulty=3),
    _c_counter("analysis", "下列哪个是第二类曲线积分？",
               [("a", "∮ P dx + Q dy"), ("b", "∮ f ds"),
                ("c", "∬ f dS"), ("d", "∭ f dV")], "a",
               "曲线积分",
               "第二类曲线积分带方向，形式为 ∫P dx+Q dy；∮f ds 是第一类曲线积分。",
               difficulty=2),
    _c_counter("analysis", "下列哪个公式把平面闭曲线积分与二重积分联系起来？",
               [("a", "Green 公式"), ("b", "Newton–Leibniz 公式"),
                ("c", "Taylor 公式"), ("d", "Cauchy–Schwarz 不等式")], "a",
               "Green 公式",
               "Green 公式：∮_∂D P dx+Q dy = ∬_D (Q_x-P_y) dA。",
               difficulty=2),
    _c_counter("analysis", "下列哪个级数收敛？",
               [("a", "∑ 1/n²"), ("b", "∑ 1/n"),
                ("c", "∑ 1/√n"), ("d", "∑ n")], "a",
               "级数",
               "∑1/n² 是 p=2>1 的 p 级数，收敛；∑1/n 与 ∑1/√n 发散，∑n 通项不趋于 0。",
               difficulty=2,
               scene="数值逼近与误差估计"),
    _c_counter("analysis", "下列哪个反常积分收敛？",
               [("a", "∫_1^∞ 1/x² dx"), ("b", "∫_1^∞ 1/x dx"),
                ("c", "∫_0^1 1/x dx"), ("d", "∫_0^∞ 1 dx")], "a",
               "反常积分",
               "∫_1^∞ x^{-2} dx = 1 收敛；1/x 与 1 在无穷区间发散，1/x 在 0 附近也发散。",
               difficulty=2),
    _c_counter("analysis", "下列哪个函数在 [-1,1] 上黎曼可积但不连续？",
               [("a", "阶跃函数"), ("b", "x²"),
                ("c", "sin x"), ("d", "e^x")], "a",
               "黎曼积分",
               "阶跃函数只有有限个跳变点，仍然黎曼可积，但在跳变点不连续。",
               difficulty=2),
    _c_tf("analysis", "若连续函数列在区间上一致收敛，则极限函数连续。", True,
          "级数",
          "一致收敛保证极限与连续可以交换：对任意 ε，先取足够大的 n 使 |f_n-f| 一致小，"
          "再用 f_n 的连续性控制 f 的增量。",
          difficulty=3,
          hints=["一致收敛比逐点收敛强在哪里？", "尝试把 |f(x)-f(x0)| 拆成三项。"],
          scene="函数项级数、逼近与数值分析"),
    _c_counter("analysis", "下列哪个级数条件收敛？",
               [("a", "∑ (-1)^n/n"), ("b", "∑ 1/n²"),
                ("c", "∑ 1/n"), ("d", "∑ (-1)^n")], "a",
               "级数",
               "∑(-1)^n/n 收敛（Leibniz），但取绝对值后是调和级数，发散，所以条件收敛。",
               difficulty=3),

    _c_tf("algebra", "若 f(a)=f'(a)=0，则 a 是多项式 f 的重根（至少二重）。", True,
          "多项式",
          "在特征 0 的域上，a 是 k 重根当且仅当 f(a)=f'(a)=…=f^{(k-1)}(a)=0。",
          difficulty=2),
    _c_tf("algebra", "两个多项式的最大公因式在相差非零常数倍意义下唯一。", True,
          "多项式",
          "最大公因式不唯一，但首一化后唯一；这是多项式环是欧几里得整环的体现。",
          difficulty=2),
    _c_tf("algebra", "有理系数多项式若有有理根 p/q（既约），则 p 整除常数项，q 整除首项系数。", True,
          "多项式",
          "这是有理根定理；把它代入并比较整除关系即可证明。",
          difficulty=3,
          hints=["先把根写成既约分数 p/q。", "比较首项与常数项的整除关系。"],
          scene="多项式求根与因式分解"),
    _c_counter("algebra", "下列哪个多项式在 Q 上不可约？",
               [("a", "x²-2"), ("b", "x²-1"),
                ("c", "x²+2x+1"), ("d", "x²-4")], "a",
               "多项式",
               "x²-2 在 Q 上无有理根，因此不可约；其余三个都能分解。",
               difficulty=2),
    _c_counter("algebra", "下列哪个是 x³-1 的因式？",
               [("a", "x-1"), ("b", "x+1"),
                ("c", "x-2"), ("d", "x²+1")], "a",
               "多项式",
               "x³-1=(x-1)(x²+x+1)，所以 x-1 是因式。",
               difficulty=1),

    _c_tf("algebra", "线性变换在不同基下的矩阵是相似的。", True,
          "线性变换",
          "换基矩阵 P 满足 B=P^{-1}AP，这正是相似的定义。",
          difficulty=2,
          scene="坐标变换、机器人姿态与量子力学表象"),
    _c_tf("algebra", "相似矩阵有相同的特征值（计重数）。", True,
          "线性变换",
          "相似矩阵有相同的特征多项式，因此特征值及重数相同。",
          difficulty=2),
    _c_tf("algebra", "实二次型一定可以通过正交变换化为标准形。", True,
          "二次型",
          "实对称矩阵可正交对角化，因此二次型在正交变换下化为 λ1y1²+…+λnyn²。",
          difficulty=3),
    _c_tf("algebra", "二次型正定的充要条件是所有顺序主子式都大于 0。", True,
          "二次型",
          "Sylvester 判据：实对称矩阵正定 ⇔ 所有顺序主子式为正。",
          difficulty=3,
          hints=["先写出二次型的对称矩阵。", "回忆 Sylvester 正定判据。"],
          scene="最优化中的凸性与稳定性"),
    _c_tf("algebra", "每个复矩阵都相似于一个 Jordan 标准形。", True,
          "Jordan 标准形",
          "代数闭域上，线性变换可以分解为广义特征空间，并在每个空间上取 Jordan 块。",
          difficulty=4),
    _c_counter("algebra", "下列哪个二次型是正定的？",
               [("a", "x²+y²"), ("b", "x²-y²"),
                ("c", "xy"), ("d", "-x²-y²")], "a",
               "二次型",
               "x²+y² 的矩阵是单位阵，顺序主子式都为正；其余分别不定、不定、负定。",
               difficulty=2),
    _c_counter("algebra", "下列哪组矩阵相似？",
               [("a", "[[1,1],[0,2]] 与 diag(1,2)"),
                ("b", "[[1,1],[0,1]] 与 I"),
                ("c", "diag(1,2) 与 diag(1,3)"),
                ("d", "[[0,1],[0,0]] 与零矩阵")], "a",
               "线性变换",
               "a 中矩阵有互异特征值 1、2，可对角化，因此相似于 diag(1,2)；"
               "b、d 的 Jordan 结构不同，c 的特征值不同。",
               difficulty=3),
]

# ------------------------------------------------------------
# 计算题生成器（答案由代码算出，测试会用独立算法复算）
# ------------------------------------------------------------

def _gcd(a, b):
    while b:
        a, b = b, a % b
    return abs(a)


def _phi(n):
    out, m, p = n, n, 2
    while p * p <= m:
        if m % p == 0:
            while m % p == 0:
                m //= p
            out -= out // p
        p += 1
    if m > 1:
        out -= out // m
    return out


def _divisor_count(n):
    count, p = 0, 1
    while p * p <= n:
        if n % p == 0:
            count += 1 if p * p == n else 2
        p += 1
    return count


def _rank(matrix):
    m = [[Fraction(x) for x in row] for row in matrix]
    rows = len(m)
    cols = len(m[0]) if rows else 0
    rank = 0
    for c in range(cols):
        pivot = next((r for r in range(rank, rows) if m[r][c] != 0), None)
        if pivot is None:
            continue
        m[rank], m[pivot] = m[pivot], m[rank]
        pv = m[rank][c]
        m[rank] = [x / pv for x in m[rank]]
        for r in range(rows):
            if r != rank and m[r][c] != 0:
                f = m[r][c]
                m[r] = [a - f * b for a, b in zip(m[r], m[rank])]
        rank += 1
        if rank == rows:
            break
    return rank


def _int_answer(value, prompt, explain, skill, difficulty,
                hints=None, steps=None, exam_tip="", scene=""):
    q = {"kind": "calc", "prompt": prompt, "answer": value,
         "explain": explain, "skill": skill, "difficulty": difficulty,
         "unit": "", "ai_hint": explain}
    if hints:
        q["hints"] = hints
    if steps:
        q["steps"] = steps
    if exam_tip:
        q["exam_tip"] = exam_tip
    if scene:
        q["scene"] = scene
    return q


def _float_answer(value, prompt, explain, skill, difficulty, unit="",
                  hints=None, steps=None, exam_tip="", scene=""):
    q = {"kind": "calc", "prompt": prompt, "answer": float(value),
         "explain": explain, "skill": skill, "difficulty": difficulty,
         "unit": unit, "ai_hint": explain}
    if hints:
        q["hints"] = hints
    if steps:
        q["steps"] = steps
    if exam_tip:
        q["exam_tip"] = exam_tip
    if scene:
        q["scene"] = scene
    return q


def _calc_number(rng, level):
    pick = rng.choice(["gcd", "phi", "inv", "powmod", "divisors", "crt", "legendre"])
    if pick == "gcd":
        a = rng.randint(40, 200) * (3 + level)
        b = rng.randint(40, 200) * (3 + level)
        g = _gcd(a, b)
        return _int_answer(g, "gcd(%d, %d) = ?" % (a, b),
                           "用辗转相除法：反复用余数替换较大数，最后的非零余数就是 %d。" % g,
                           "最大公因数", min(5, 1 + level // 2))
    if pick == "phi":
        n = rng.choice([36, 48, 60, 72, 84, 96, 100, 108, 120, 144, 150, 180])
        val = _phi(n)
        return _int_answer(val, "φ(%d) = ?" % n,
                           "φ(n)=n·∏(1-1/p)，把 %d 分解后逐项相减，得到 %d。" % (n, val),
                           "欧拉函数", min(5, 2 + level // 2))
    if pick == "inv":
        m = rng.choice([7, 11, 13, 17, 19, 23, 29, 31])
        a = rng.choice([x for x in range(2, m) if _gcd(x, m) == 1])
        val = pow(a, -1, m)
        return _int_answer(val, "%d 在模 %d 下的逆元是？" % (a, m),
                           "用扩展欧几里得求 ax+%dy=1，x 取模 %d 即得 %d。" % (m, m, val),
                           "模逆", min(5, 2 + level // 2))
    if pick == "powmod":
        a = rng.randint(2, 9)
        b = rng.randint(5, 12)
        m = rng.choice([7, 11, 13, 17, 19, 23])
        val = pow(a, b, m)
        return _int_answer(val, "%d^%d mod %d = ?" % (a, b, m),
                           "用快速幂逐次平方取模，结果是 %d。" % val,
                           "模幂", min(5, 2 + level // 2))
    if pick == "divisors":
        n = rng.randint(60, 240)
        val = _divisor_count(n)
        return _int_answer(val, "%d 的正因数个数是？" % n,
                           "分解质因数后把各指数加 1 相乘，得到 %d。" % val,
                           "因数个数", min(5, 1 + level // 2))
    if pick == "crt":
        m = rng.choice([3, 4, 5, 7, 8, 9, 11])
        n = rng.choice([x for x in [3, 4, 5, 7, 8, 9, 11] if _gcd(x, m) == 1 and x != m])
        a = rng.randint(0, m - 1)
        b = rng.randint(0, n - 1)
        val = next(x for x in range(m * n) if x % m == a and x % n == b)
        return _int_answer(val,
                           "解同余方程组：x≡%d (mod %d)，x≡%d (mod %d)，求最小非负解。" % (a, m, b, n),
                           "中国剩余定理：在 0..%d 内找同时满足两式的最小解，得到 %d。" % (m * n - 1, val),
                           "中国剩余定理", min(5, 2 + level // 2))
    p = rng.choice([5, 7, 11, 13, 17, 19])
    a = rng.randint(1, p - 1)
    e = pow(a, (p - 1) // 2, p)
    val = 0 if e == 0 else (1 if e == 1 else -1)
    return _int_answer(val, "勒让德符号 (%d/%d) = ?（1 / -1 / 0）" % (a, p),
                       "用欧拉判别法：计算 a^((p-1)/2) mod p，得到 %d。" % val,
                       "二次剩余", min(5, 3 + level // 2))


def _calc_algebra(rng, level, picks=None):
    pick = rng.choice(picks or ["det2", "det3", "rank", "eigen", "trace", "solve", "nullity"])
    if pick == "det2":
        a, b, c, d = [rng.randint(-9, 9) for _ in range(4)]
        val = a * d - b * c
        return _int_answer(val, "det([[%d,%d],[%d,%d]]) = ?" % (a, b, c, d),
                           "二阶行列式等于主对角线乘积减去副对角线乘积：%d。" % val,
                           "行列式", min(5, 1 + level // 2))
    if pick == "det3":
        m = [[rng.randint(-5, 5) for _ in range(3)] for _ in range(3)]
        val = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
               - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
               + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
        return _int_answer(val, "det(%s) = ?" % m,
                           "按第一行展开，三项代数余子式相加，得到 %d。" % val,
                           "行列式", min(5, 2 + level // 2))
    if pick == "rank":
        m = [[rng.randint(-4, 4) for _ in range(3)] for _ in range(3)]
        val = _rank(m)
        return _int_answer(val, "矩阵 %s 的秩是？" % m,
                           "做初等行变换化为阶梯形，非零行数就是秩：%d。" % val,
                           "矩阵的秩", min(5, 2 + level // 2))
    if pick == "eigen":
        for _ in range(200):
            a, b, c, d = [rng.randint(-6, 6) for _ in range(4)]
            disc = (a + d) ** 2 - 4 * (a * d - b * c)
            if disc < 0:
                continue
            s = math.isqrt(disc)
            if s * s != disc:
                continue
            l1 = (a + d - s) / 2
            l2 = (a + d + s) / 2
            if abs(l1 - l2) < 1e-9:
                continue
            val = min(l1, l2)
            return _float_answer(val, "矩阵 [[%d,%d],[%d,%d]] 的较小特征值是？" % (a, b, c, d),
                                 "解特征方程 λ²-tr(A)λ+det(A)=0，较小根为 %s。" % _fmt(val),
                                 "特征值", min(5, 3 + level // 2))
        return _int_answer(1, "矩阵 [[2,0],[0,1]] 的较小特征值是？",
                           "对角矩阵的特征值就是对角元。", "特征值", 2)
    if pick == "trace":
        m = [[rng.randint(-9, 9) for _ in range(3)] for _ in range(3)]
        val = m[0][0] + m[1][1] + m[2][2]
        return _int_answer(val, "矩阵 %s 的迹是？" % m,
                           "迹等于主对角线元素之和：%d。" % val, "矩阵的迹", 1)
    if pick == "solve":
        x, y = rng.randint(-9, 9), rng.randint(-9, 9)
        while True:
            a, b, c, d = [rng.randint(-5, 5) for _ in range(4)]
            if a * d - b * c != 0:
                break
        e, f = a * x + b * y, c * x + d * y
        return _int_answer(x,
                           "解方程组：%dx+%dy=%d，%dx+%dy=%d，求 x。" % (a, b, e, c, d, f),
                           "用消元法或克拉默法则，解得 x=%d。" % x,
                           "线性方程组", min(5, 2 + level // 2))
    m = [[rng.randint(-4, 4) for _ in range(3)] for _ in range(3)]
    val = 3 - _rank(m)
    return _int_answer(val, "矩阵 %s 的零度（nullity）是？" % m,
                       "零度 = 未知数个数 - 秩 = 3 - %d = %d。" % (3 - val, val),
                       "零空间", min(5, 3 + level // 2))


def _term(coef, body):
    if not body:
        return "%+d" % coef
    if coef == 1:
        return "+" + body
    if coef == -1:
        return "-" + body
    return "%+d%s" % (coef, body)


def _poly_expr(terms):
    text = "".join(_term(c, b) for c, b in terms if c)
    return text.lstrip("+") or "0"


def _calc_analysis(rng, level, picks=None):
    pick = rng.choice(picks or ["sinx", "poly", "deriv", "intpow", "intexp", "geom", "taylor"])
    if pick == "sinx":
        a = rng.randint(2, 9)
        return _int_answer(a, "lim(x→0) sin(%dx)/x = ?" % a,
                           "利用 sin(ax)/x = a·sin(ax)/(ax) → a，所以是 %d。" % a,
                           "极限", 2)
    if pick == "poly":
        a = rng.randint(1, 9)
        d = rng.randint(1, 9)
        b, c = rng.randint(-5, 5), rng.randint(-5, 5)
        num = _poly_expr([(a, "x²"), (b, "x"), (c, "")])
        den = _poly_expr([(d, "x²"), (b, "x"), (c, "")])
        return _float_answer(
            a / d,
            "lim(x→∞) (%s)/(%s) = ?" % (num, den),
            "最高次项系数之比：%d/%d = %s。" % (a, d, _fmt(a / d)),
            "极限", 2)
    if pick == "deriv":
        n = rng.randint(2, 6)
        a = rng.randint(-3, 3)
        val = n * (a ** (n - 1))
        expr = "2x" if n == 2 else "%dx^%d" % (n, n - 1)
        return _int_answer(val, "f(x)=x^%d，f'(%d) = ?" % (n, a),
                           "f'(x)=%s，代入 x=%d 得 %d。" % (expr, a, val),
                           "导数", 2)
    if pick == "intpow":
        n = rng.randint(1, 6)
        val = 1 / (n + 1)
        return _float_answer(val, "∫₀¹ x^%d dx = ?" % n,
                             "幂函数积分：x^(%d)/(%d) 在 0 到 1 上取值，得 %s。" % (n + 1, n + 1, _fmt(val)),
                             "定积分", 2)
    if pick == "intexp":
        a = rng.randint(1, 4)
        val = (math.e ** a - 1) / a
        inner = "x" if a == 1 else "%dx" % a
        return _float_answer(
            val, "∫₀¹ e^(%s) dx = ?（保留 4 位小数）" % inner,
            "原函数是 e^(%s)/%d，代入上下限得 %s。" % (inner, a, _fmt(val)),
            "定积分", 3)
    if pick == "geom":
        r = rng.choice([(1, 2), (1, 3), (2, 3), (1, 4)])
        val = 1 / (1 - r[0] / r[1])
        return _float_answer(val, "∑(n=0→∞) (%d/%d)^n = ?" % r,
                             "等比级数求和：1/(1-r) = %s。" % _fmt(val),
                             "级数", 2)
    k = rng.randint(2, 6)
    val = 1 / math.factorial(k)
    return _float_answer(val, "e^x 的泰勒展开中，x^%d 的系数是？" % k,
                         "e^x=∑x^n/n!，所以 x^%d 的系数是 1/%d! = %s。" % (k, k, _fmt(val)),
                         "泰勒展开", 3)


def _calc_abstract(rng, level):
    pick = rng.choice(["order", "subgroups", "kernel", "sn", "order2"])
    if pick == "order":
        n = rng.randint(6, 20)
        a = rng.randint(0, n - 1)
        val = n // _gcd(n, a) if a else 1
        return _int_answer(val, "在 Z_%d 中，元素 %d 的阶是？" % (n, a),
                           "阶 = n/gcd(n,a) = %d/%d = %d。" % (n, _gcd(n, a), val),
                           "元素阶", min(5, 2 + level // 2))
    if pick == "subgroups":
        n = rng.randint(12, 60)
        val = _divisor_count(n)
        return _int_answer(val, "Z_%d 有多少个子群？" % n,
                           "循环群的子群与 n 的正因数一一对应，%d 有 %d 个因数。" % (n, val),
                           "子群", min(5, 2 + level // 2))
    if pick == "kernel":
        n = rng.randint(6, 24)
        m = rng.randint(4, 18)
        val = _gcd(n, m)
        return _int_answer(val, "同态 Z_%d → Z_%d（取模）的核有多少个元素？" % (n, m),
                           "核的大小等于 gcd(%d,%d)=%d。" % (n, m, val),
                           "同态核", min(5, 3 + level // 2))
    if pick == "sn":
        n = rng.randint(3, 6)
        val = math.factorial(n)
        return _int_answer(val, "对称群 S_%d 的阶是？" % n,
                           "|S_n|=n!，所以 |S_%d|=%d! = %d。" % (n, n, val),
                           "对称群", 1)
    k = rng.randint(2, 4)
    val = 2 ** k - 1
    return _int_answer(val, "Z_2^%d 中阶为 2 的元素有多少个？" % k,
                       "除零元外每个元素的阶都是 2，共 2^%d-1=%d 个。" % (k, val),
                       "初等交换群", min(5, 3 + level // 2))


def _arg_degrees(a, b):
    table = {(1, 1): 45, (1, -1): -45, (-1, 1): 135, (-1, -1): -135,
             (1, 0): 0, (0, 1): 90, (-1, 0): 180, (0, -1): -90}
    return table[(a, b)]


def _vec(v):
    return "(" + ", ".join(str(x) for x in v) + ")"


def _root_factor(r):
    return "(r%+d)" % (-r)


def _lp_max(a_cap, b_cap, s_cap, p, q):
    """max px+qy，约束 x,y>=0、x<=A、y<=B、x+y<=S；返回 (最优点, 最优值)。"""
    cand = [(0, 0), (a_cap, 0), (0, b_cap), (a_cap, b_cap)]
    for x in (0, a_cap):
        y = s_cap - x
        if 0 <= y <= b_cap:
            cand.append((x, y))
    for y in (0, b_cap):
        x = s_cap - y
        if 0 <= x <= a_cap:
            cand.append((x, y))
    best = None
    for x, y in cand:
        if x < 0 or y < 0 or x > a_cap or y > b_cap or x + y > s_cap:
            continue
        val = p * x + q * y
        if best is None or val > best[1]:
            best = ((x, y), val)
    return best


# ------------------------------------------------------------
# 新增课程的计算题生成器
# ------------------------------------------------------------

def _calc_probability(rng, level):
    pick = rng.choice(["bayes", "binomial", "expect", "normalz"])
    if pick == "bayes":
        prev = rng.choice([0.01, 0.02, 0.05, 0.1])
        sens = rng.choice([0.9, 0.95, 0.98, 0.99])
        spec = rng.choice([0.9, 0.95, 0.98, 0.99])
        fpr = 1 - spec
        post = prev * sens / (prev * sens + (1 - prev) * fpr)
        return _float_answer(
            round(post, 6),
            "某病患病率 %.0f%%，检测灵敏度 %.0f%%，特异度 %.0f%%。"
            "某人检测阳性，实际患病的概率约为？"
            % (prev * 100, sens * 100, spec * 100),
            "Bayes 公式：P(病|阳)=P(阳|病)P(病)/P(阳)；"
            "分母 = %.4f×%.2f + %.4f×%.2f，得到 %.4f。"
            % (prev, sens, 1 - prev, fpr, post),
            "条件概率与 Bayes", min(5, 3 + level // 2),
            hints=["先写 P(病|阳)=P(阳|病)P(病)/P(阳)。",
                   "分母用全概率：患病且阳性 + 健康但假阳性。"],
            steps=["先算患病且阳性的概率：%.4f×%.2f=%.4f。" % (prev, sens, prev * sens),
                   "再算健康但阳性的概率：%.4f×%.2f=%.4f。" % (1 - prev, fpr, (1 - prev) * fpr),
                   "两者相除得到后验概率 %.4f。" % post],
            exam_tip="先分清 P(阳|病) 与 P(病|阳)，不要把条件方向写反。",
            scene="医学检测、欺诈检测中的基率谬误")
    if pick == "binomial":
        n = rng.randint(5, 12)
        p = rng.choice([0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
        k = rng.randint(0, n)
        val = math.comb(n, k) * (p ** k) * ((1 - p) ** (n - k))
        return _float_answer(
            round(val, 6),
            "X~B(%d, %.1f)，P(X=%d) = ?" % (n, p, k),
            "二项分布：P(X=k)=C(n,k)p^k(1-p)^(n-k)，"
            "C(%d,%d)=%d，代入得 %.6f。" % (n, k, math.comb(n, k), val),
            "二项分布", min(5, 2 + level // 2),
            hints=["二项分布要选 k 次成功、n-k 次失败。",
                   "别漏掉组合数 C(n,k)。"],
            steps=["组合数 C(%d,%d)=%d。" % (n, k, math.comb(n, k)),
                   "成功概率 %.1f^%d，失败概率 %.1f^%d。" % (p, k, 1 - p, n - k),
                   "三者相乘得 %.6f。" % val],
            scene="抽检、投票、重复实验中的成功次数")
    if pick == "expect":
        vals = rng.sample(range(-3, 8), 3)
        probs = rng.choice([[0.2, 0.3, 0.5], [0.1, 0.3, 0.6], [0.25, 0.35, 0.4]])
        probs = probs[:]
        rng.shuffle(probs)
        val = sum(v * p for v, p in zip(vals, probs))
        table = "，".join("%d: %.2f" % (v, p) for v, p in zip(vals, probs))
        return _float_answer(
            round(val, 6),
            "离散随机变量 X 的分布为 %s。E(X) = ?" % table,
            "E(X)=Σ x·P(X=x)=%s=%.4f。"
            % (" + ".join("%d×%.2f" % (v, p) for v, p in zip(vals, probs)), val),
            "数学期望", min(5, 2 + level // 2),
            hints=["期望是「取值 × 概率」再求和。",
                   "概率之和应为 1，先检查分布表。"],
            steps=["逐项算 x·P(X=x)：%s。"
                   % "，".join("%d×%.2f=%g" % (v, p, v * p) for v, p in zip(vals, probs)),
                   "把所有项相加得到 %.4f。" % val],
            scene="保险赔付、抽奖与风险决策的期望收益")
    mu = rng.randint(50, 100)
    sigma = rng.choice([2, 4, 5, 10])
    k = rng.choice([-2, -1, 1, 2])
    x = mu + k * sigma
    return _int_answer(
        k,
        "X~N(%d, %d²)，求 x=%d 标准化后的 z 值（z=(x-μ)/σ）。" % (mu, sigma, x),
        "z=(%d-%d)/%d=%d。" % (x, mu, sigma, k),
        "正态分布标准化", min(5, 2 + level // 2),
        hints=["标准化公式 z=(x-μ)/σ。", "σ 是标准差，不是方差。"],
        steps=["先算偏差 x-μ=%d。" % (x - mu),
               "再除以标准差 %d，得到 z=%d。" % (sigma, k)],
        exam_tip="先确认题目给的是方差还是标准差；标准化只除标准差。",
        scene="质量控制、考试成绩与测量误差")


def _calc_statistics(rng, level):
    pick = rng.choice(["mean", "var", "mle", "ci"])
    if pick in ("mean", "var"):
        data = [rng.randint(1, 20) for _ in range(5)]
        mean = sum(data) / len(data)
        text = "，".join(str(x) for x in data)
        if pick == "mean":
            return _float_answer(
                round(mean, 6), "样本数据：%s。样本均值是多少？" % text,
                "均值 = (%s)/5 = %.4f。" % ("+".join(str(x) for x in data), mean),
                "样本均值", min(5, 1 + level // 2),
                hints=["把所有观测值相加，再除以样本量。", "样本量是 5。"],
                steps=["求和：%s=%d。" % ("+".join(str(x) for x in data), sum(data)),
                       "除以 5 得 %.4f。" % mean],
                scene="用样本平均收入、平均分估计总体水平")
        var = sum((x - mean) ** 2 for x in data) / (len(data) - 1)
        return _float_answer(
            round(var, 6), "样本数据：%s。无偏样本方差 S² 是多少？" % text,
            "S²=Σ(xi-x̄)²/(n-1)，先算均值 %.4f，再除以 4 得 %.4f。" % (mean, var),
            "无偏样本方差", min(5, 2 + level // 2),
            hints=["无偏样本方差的分母是 n-1。", "先求均值，再算偏差平方和。"],
            steps=["样本均值 x̄=%.4f。" % mean,
                   "偏差平方和 = %.4f。" % sum((x - mean) ** 2 for x in data),
                   "除以 n-1=4 得 %.4f。" % var],
            exam_tip="看到「无偏样本方差」就用 n-1；总体方差才用 n。",
            scene="质量控制中的过程波动估计")
    if pick == "mle":
        n = rng.randint(10, 50)
        k = rng.randint(0, n)
        p = k / n
        return _float_answer(
            round(p, 6),
            "%d 次独立伯努利试验中成功 %d 次。成功概率 p 的极大似然估计是？" % (n, k),
            "伯努利分布的 MLE 就是成功频率：p̂=%d/%d=%.4f。" % (k, n, p),
            "极大似然估计", min(5, 2 + level // 2),
            hints=["写出似然函数 L(p)=p^k(1-p)^(n-k)。", "对 log L 求导并令其为 0。"],
            steps=["对数似然 ln L=k ln p+(n-k)ln(1-p)。",
                   "求导得 k/p-(n-k)/(1-p)=0。",
                   "解得 p̂=k/n=%.4f。" % p],
            exam_tip="伯努利/二项模型的 MLE 就是样本成功率；先写似然再求导。",
            scene="点击率、转化率与良品率的估计")
    sigma = rng.choice([2, 4, 5, 10])
    n = rng.choice([16, 25, 36, 49, 64, 100])
    half = 1.96 * sigma / math.sqrt(n)
    return _float_answer(
        round(half, 6),
        "已知总体标准差 σ=%d，样本量 n=%d。总体均值的 95%% 置信区间半宽约为？"
        % (sigma, n),
        "半宽 = 1.96·σ/√n = 1.96×%d/√%d = %.4f。" % (sigma, n, half),
        "置信区间", min(5, 3 + level // 2),
        hints=["95% 正态区间用临界值 1.96。", "标准误是 σ/√n，不是 σ/n。"],
        steps=["标准误 = %d/√%d = %.4f。" % (sigma, n, sigma / math.sqrt(n)),
               "乘以 1.96 得半宽 %.4f。" % half],
        exam_tip="区间估计先写标准误；95% 用 1.96，99% 用 2.576。",
        scene="A/B 测试与民意调查的误差范围")


def _calc_ode(rng, level):
    pick = rng.choice(["growth", "linear", "roots", "equilibrium"])
    if pick == "growth":
        k = rng.choice([0.1, 0.2, 0.3, -0.1, -0.2])
        y0 = rng.randint(1, 10)
        t = rng.randint(1, 5)
        val = y0 * math.exp(k * t)
        return _float_answer(
            round(val, 6),
            "解初值问题 y'=%.1fy，y(0)=%d，求 y(%d)（保留 4 位小数）。"
            % (k, y0, t),
            "可分离方程的解为 y=y0·e^{kt}=%d·e^{%.1f×%d}=%.4f。"
            % (y0, k, t, val),
            "可分离方程", min(5, 2 + level // 2),
            hints=["把 y'=ky 分离为 dy/y=k dt。", "两边积分后代入初值定常数。"],
            steps=["分离变量并积分：ln|y|=kt+C。",
                   "由 y(0)=%d 得 C=ln %d。" % (y0, y0),
                   "所以 y(%d)=%.4f。" % (t, val)],
            scene="人口增长、复利、药物代谢与放射性衰变")
    if pick == "linear":
        a = rng.choice([0.5, 1, 2, 3])
        r = rng.randint(1, 10)
        b = a * r
        y0 = rng.randint(0, 10)
        t = rng.randint(1, 4)
        val = r + (y0 - r) * math.exp(-a * t)
        return _float_answer(
            round(val, 6),
            "解初值问题 y'+%.1fy=%.1f，y(0)=%d，求 y(%d)（保留 4 位小数）。"
            % (a, b, y0, t),
            "平衡解为 b/a=%.1f，解为 y=%.1f+(%d-%.1f)e^{-%.1ft}，代入得 %.4f。"
            % (r, r, y0, r, a, val),
            "一阶线性方程", min(5, 3 + level // 2),
            hints=["先求平衡解 y=b/a。", "解的形式是平衡解 + 初值偏差的指数衰减。"],
            steps=["平衡解 y*=b/a=%.1f。" % r,
                   "通解 y=y*+(y0-y*)e^{-at}。",
                   "代入 t=%d 得 %.4f。" % (t, val)],
            scene="冷却、RC 电路与一阶惯性系统")
    if pick == "roots":
        r1 = rng.randint(-4, 4)
        r2 = rng.randint(-4, 4)
        while r2 == r1:
            r2 = rng.randint(-4, 4)
        b = -(r1 + r2)
        c = r1 * r2
        small = min(r1, r2)
        eq = "y''"
        if b:
            eq += " %+d y'" % b
        if c:
            eq += " %+d y" % c
        expr_b = ("%+d r" % b) if b else ""
        expr_c = ("%+d" % c) if c else ""
        return _int_answer(
            small,
            "方程 %s = 0 的两个特征根中较小的是？" % eq,
            "特征方程 r²%s%s=0，因式分解为 %s%s=0，根为 %d、%d。"
            % (expr_b, expr_c, _root_factor(r1), _root_factor(r2), r1, r2),
            "常系数线性方程", min(5, 2 + level // 2),
            hints=["把 y=e^{rx} 代入，得到特征方程。", "先算判别式，再求两个根。"],
            steps=["特征方程 r²%s%s=0。" % (expr_b, expr_c),
                   "因式分解得 %s%s=0。" % (_root_factor(r1), _root_factor(r2)),
                   "较小根为 %d。" % small],
            exam_tip="先写特征方程；根的类型决定通解是指数、重根还是三角函数。")
    a = rng.choice([-3, -2, -1, 1, 2, 3])
    r = rng.randint(-5, 5)
    return _int_answer(
        r,
        "自治方程 y'=%d(y-%d) 的平衡点是？" % (a, r),
        "令 y'=0，得 y=%d。" % r,
        "平衡点", min(5, 1 + level // 2),
        hints=["平衡点满足 y'=0。", "解一次方程即可。"],
        steps=["令 %d(y-%d)=0。" % (a, r), "得到 y=%d。" % r],
        scene="种群平衡、温度平衡与市场均衡")


def _calc_complex(rng, level):
    pick = rng.choice(["mod", "arg", "power", "residue"])
    if pick == "mod":
        a, b = rng.randint(-6, 6), rng.randint(-6, 6)
        while a == 0 and b == 0:
            a, b = rng.randint(-6, 6), rng.randint(-6, 6)
        val = math.hypot(a, b)
        return _float_answer(
            round(val, 6), "z=%d%+di，|z| = ?" % (a, b),
            "|z|=√(a²+b²)=√(%d+%d)=%.4f。" % (a * a, b * b, val),
            "复数的模", min(5, 1 + level // 2),
            hints=["复数的模是实部与虚部平方和的平方根。", "注意虚部系数 b 可能为负。"],
            steps=["实部平方 %d，虚部平方 %d。" % (a * a, b * b),
                   "相加后开方得 %.4f。" % val],
            scene="交流电的幅值与信号能量")
    if pick == "arg":
        a, b = rng.choice([(1, 1), (1, -1), (-1, 1), (-1, -1),
                           (1, 0), (0, 1), (-1, 0), (0, -1)])
        val = _arg_degrees(a, b)
        return _int_answer(
            val, "z=%d%+di 的辐角主值（单位：度）是？" % (a, b),
            "点 (%d,%d) 所在象限对应辐角 %d°。" % (a, b, val),
            "复数的辐角", min(5, 2 + level // 2),
            hints=["辐角主值通常取 (-180°, 180°]。", "先看实部、虚部的符号确定象限。"],
            steps=["实部 %d，虚部 %d。" % (a, b),
                   "对应象限的角为 %d°。" % val],
            exam_tip="辐角别只看 atan(b/a)，要先判象限再修正。")
    if pick == "power":
        a, b = rng.choice([(1, 1), (1, -1), (2, 1), (1, 2)])
        n = rng.randint(2, 4)
        real, imag = 1, 0
        for _ in range(n):
            real, imag = real * a - imag * b, real * b + imag * a
        which = rng.choice(["实部", "虚部"])
        val = real if which == "实部" else imag
        return _int_answer(
            val, "z=%d%+di，z^%d 的%s是？" % (a, b, n, which),
            "用复数乘法或 De Moivre 公式逐步计算，z^%d=%d%+di。"
            % (n, real, imag),
            "复数的乘方", min(5, 2 + level // 2),
            hints=["先算 z²，再继续乘到 z^n。", "用 (a+bi)(c+di)=(ac-bd)+(ad+bc)i。"],
            steps=["从 z¹=%d%+di 开始逐次乘 z。" % (a, b),
                   "第 %d 次乘方得到 %d%+di。" % (n, real, imag),
                   "%s为 %d。" % (which, val)],
            scene="旋转与周期信号")
    z0 = rng.randint(-3, 3)
    a = rng.choice([-3, -2, -1, 1, 2, 3])
    b = rng.randint(-5, 5)
    val = a * z0 + b
    return _int_answer(
        val,
        "f(z)=(%dz%+d)/(z%+d) 在 z=%d 处的留数是？" % (a, b, -z0, z0),
        "z=%d 是一阶极点，留数 = 分子的极限 = %d×%d%+d = %d。"
        % (z0, a, z0, b, val),
        "留数", min(5, 3 + level // 2),
        hints=["一阶极点 z0 的留数 = lim (z-z0)f(z)。", "把 z0 代入分子即可。"],
        steps=["确认 z=%d 是一阶极点。" % z0,
               "留数 = lim (z-%d)f(z)。" % z0,
               "代入得 %d。" % val],
        scene="围道积分与信号系统的极点分析")


def _calc_geometry(rng, level):
    pick = rng.choice(["dot", "cross", "dist", "angle"])
    u = [rng.randint(-5, 5) for _ in range(3)]
    v = [rng.randint(-5, 5) for _ in range(3)]
    if pick == "dot":
        val = sum(a * b for a, b in zip(u, v))
        return _int_answer(
            val, "u=%s，v=%s，u·v = ?" % (_vec(u), _vec(v)),
            "点积等于对应分量乘积之和：%d。" % val,
            "向量点积", min(5, 1 + level // 2),
            hints=["u·v=u1v1+u2v2+u3v3。", "逐项相乘再相加，注意负号。"],
            steps=["对应分量相乘：%s。"
                   % "，".join("%d×%d" % (a, b) for a, b in zip(u, v)),
                   "相加得 %d。" % val],
            scene="功=力·位移、推荐系统相似度")
    if pick == "cross":
        cx = u[1] * v[2] - u[2] * v[1]
        cy = u[2] * v[0] - u[0] * v[2]
        cz = u[0] * v[1] - u[1] * v[0]
        val = math.sqrt(cx * cx + cy * cy + cz * cz)
        return _float_answer(
            round(val, 6), "u=%s，v=%s，|u×v| = ?" % (_vec(u), _vec(v)),
            "叉积 u×v=(%d,%d,%d)，其模为 %.4f。" % (cx, cy, cz, val),
            "向量叉积", min(5, 2 + level // 2),
            hints=["先算叉积的三个分量。", "|u×v| 等于两向量张成平行四边形的面积。"],
            steps=["cx=u2v3-u3v2=%d。" % cx,
                   "cy=u3v1-u1v3=%d，cz=u1v2-u2v1=%d。" % (cy, cz),
                   "模为 √(%d²+%d²+%d²)=%.4f。" % (cx, cy, cz, val)],
            scene="力矩、法向量与图形学中的面积")
    if pick == "dist":
        a, b, c = [rng.randint(-3, 3) for _ in range(3)]
        while a == b == c == 0:
            a, b, c = [rng.randint(-3, 3) for _ in range(3)]
        d = rng.randint(-5, 5)
        p = [rng.randint(-4, 4) for _ in range(3)]
        num = abs(a * p[0] + b * p[1] + c * p[2] + d)
        den = math.sqrt(a * a + b * b + c * c)
        val = num / den
        return _float_answer(
            round(val, 6),
            "点 P%s 到平面 %dx%+dy%+dz%+d=0 的距离是？"
            % (_vec(p), a, b, c, d),
            "d=|ax0+by0+cz0+d|/√(a²+b²+c²)=%d/%.4f=%.4f。"
            % (num, den, val),
            "点到平面距离", min(5, 2 + level // 2),
            hints=["代入点到平面距离公式。", "分母是法向量 (a,b,c) 的模。"],
            steps=["分子 |%d×%d%+d×%d%+d×%d%+d|=%d。"
                   % (a, p[0], b, p[1], c, p[2], d, num),
                   "分母 √(%d²+%d²+%d²)=%.4f。" % (a, b, c, den),
                   "相除得 %.4f。" % val],
            scene="计算机图形学、机器人避障与误差度量")
    u = [rng.randint(-3, 3) for _ in range(3)]
    v = [rng.randint(-3, 3) for _ in range(3)]
    while sum(x * x for x in u) == 0 or sum(x * x for x in v) == 0:
        u = [rng.randint(-3, 3) for _ in range(3)]
        v = [rng.randint(-3, 3) for _ in range(3)]
    dot = sum(a * b for a, b in zip(u, v))
    nu = math.sqrt(sum(a * a for a in u))
    nv = math.sqrt(sum(b * b for b in v))
    cosv = dot / (nu * nv)
    return _float_answer(
        round(cosv, 6), "u=%s，v=%s，两向量夹角的余弦是？" % (_vec(u), _vec(v)),
        "cos θ=(u·v)/(|u||v|)=%d/(%.4f×%.4f)=%.4f。"
        % (dot, nu, nv, cosv),
        "向量夹角", min(5, 2 + level // 2),
        hints=["cos θ=(u·v)/(|u||v|)。", "先算点积，再分别算两个模。"],
        steps=["u·v=%d。" % dot,
               "|u|=%.4f，|v|=%.4f。" % (nu, nv),
               "cos θ=%.4f。" % cosv],
        scene="相似度、光照模型与姿态估计")


def _calc_numerical(rng, level):
    pick = rng.choice(["bisect", "newton", "trap", "simpson", "lagrange"])
    if pick == "bisect":
        c = rng.randint(2, 100)
        a = 1
        b = math.isqrt(c) + 2
        tol = rng.choice([1e-2, 1e-3, 1e-4])
        n = max(1, math.ceil(math.log2((b - a) / tol)))
        return _int_answer(
            n,
            "用二分法求 x²-%d=0 在 [%d,%d] 内达到 %.0e 精度，至少需要多少次迭代？"
            % (c, a, b, tol),
            "每次区间减半，需要 (b-a)/2^n ≤ tol，解得 n≥ceil(log2((b-a)/tol))=%d。" % n,
            "二分法", min(5, 3 + level // 2),
            hints=["每次迭代误差上限减半。", "解 (b-a)/2^n ≤ tol。"],
            steps=["初始区间长度 %d。" % (b - a),
                   "要求 %d/2^n ≤ %.0e。" % (b - a, tol),
                   "取对数得 n=%d。" % n],
            scene="工程求根与容差控制")
    if pick == "newton":
        c = rng.randint(2, 100)
        x0 = math.isqrt(c) + rng.randint(1, 5)
        x1 = (x0 + c / x0) / 2
        return _float_answer(
            round(x1, 6),
            "用 Newton 法解 f(x)=x²-%d=0，取 x0=%d，求 x1。"
            % (c, x0),
            "x1=x0-f(x0)/f'(x0)=x0-(x0²-%d)/(2x0)=(x0+%d/x0)/2=%.4f。"
            % (c, c, x1),
            "Newton 法", min(5, 2 + level // 2),
            hints=["Newton 迭代：x_{n+1}=x_n-f(x_n)/f'(x_n)。", "本题 f'(x)=2x。"],
            steps=["f(x0)=%d²-%d=%d。" % (x0, c, x0 * x0 - c),
                   "f'(x0)=2×%d=%d。" % (x0, 2 * x0),
                   "x1=%.4f。" % x1],
            scene="快速求平方根与工程迭代")
    a = rng.randint(-2, 3)
    b = a + rng.randint(1, 3)
    p, q, r = rng.randint(1, 3), rng.randint(-3, 3), rng.randint(-5, 5)
    f = lambda x: p * x * x + q * x + r
    if pick == "trap":
        val = (b - a) * (f(a) + f(b)) / 2
        return _float_answer(
            round(val, 6),
            "用单区间梯形公式近似 ∫_%d^%d (%dx²%+dx%+d) dx。" % (a, b, p, q, r),
            "T=(b-a)[f(a)+f(b)]/2=%d×%.4f/2=%.4f。"
            % (b - a, f(a) + f(b), val),
            "梯形公式", min(5, 2 + level // 2),
            hints=["梯形公式 T=(b-a)(f(a)+f(b))/2。", "先分别算 f(a) 与 f(b)。"],
            steps=["f(%d)=%d，f(%d)=%d。" % (a, f(a), b, f(b)),
                   "区间长度 %d。" % (b - a),
                   "T=%.4f。" % val],
            scene="工程积分、累积流量与面积估算")
    if pick == "simpson":
        s = rng.randint(-3, 3)
        f3 = lambda x: p * x ** 3 + q * x * x + r * x + s
        mid = (a + b) / 2
        val = (b - a) * (f3(a) + 4 * f3(mid) + f3(b)) / 6
        return _float_answer(
            round(val, 6),
            "用 Simpson 公式近似 ∫_%d^%d (%dx³%+dx²%+dx%+d) dx。"
            % (a, b, p, q, r, s),
            "S=(b-a)[f(a)+4f(mid)+f(b)]/6=%d×%.4f/6=%.4f。"
            % (b - a, f3(a) + 4 * f3(mid) + f3(b), val),
            "Simpson 公式", min(5, 3 + level // 2),
            hints=["Simpson 公式 S=(b-a)[f(a)+4f(m)+f(b)]/6。", "m 是区间中点。"],
            steps=["中点 m=%.1f，f(m)=%.4f。" % (mid, f3(mid)),
                   "f(a)+4f(m)+f(b)=%.4f。" % (f3(a) + 4 * f3(mid) + f3(b)),
                   "乘以 (b-a)/6 得 %.4f。" % val],
            scene="科学计算中的高精度积分")
    y0, y1, y2 = [rng.randint(-5, 5) for _ in range(3)]
    val = y0 - 3 * y1 + 3 * y2
    return _int_answer(
        val,
        "已知过 (0,%d)、(1,%d)、(2,%d) 的二次插值多项式，求 x=3 处的值。"
        % (y0, y1, y2),
        "在节点 0,1,2 上，x=3 的 Lagrange 基函数值为 1、-3、3，"
        "所以 P(3)=%d-3×%d+3×%d=%d。" % (y0, y1, y2, val),
        "Lagrange 插值", min(5, 3 + level // 2),
        hints=["二次插值在三个节点上唯一。", "算 x=3 处的三个 Lagrange 基函数值。"],
        steps=["L0(3)=(3-1)(3-2)/((0-1)(0-2))=1。",
               "L1(3)=(3-0)(3-2)/((1-0)(1-2))=-3。",
               "L2(3)=(3-0)(3-1)/((2-0)(2-1))=3。",
               "P(3)=%d-3×%d+3×%d=%d。" % (y0, y1, y2, val)],
        scene="实验数据补点与传感器标定")


def _calc_modeling(rng, level):
    pick = rng.choice(["compound", "bayes", "cooling", "lp", "eoq"])
    if pick == "compound":
        p0 = rng.choice([10000, 20000, 50000, 100000])
        rate = rng.choice([0.03, 0.04, 0.05, 0.06, 0.08])
        n = rng.randint(5, 20)
        val = p0 * (1 + rate) ** n
        return _float_answer(
            round(val, 2),
            "本金 %d 元，年利率 %.0f%%，按年复利，%d 年后的本息和约为多少元？"
            % (p0, rate * 100, n),
            "A=P(1+r)^n=%d×(1+%.2f)^%d=%.2f。" % (p0, rate, n, val),
            "复利模型", min(5, 1 + level // 2),
            hints=["按年复利公式 A=P(1+r)^n。", "r 用小数，不要写成百分数。"],
            steps=["写出公式 A=P(1+r)^n。",
                   "代入 P=%d、r=%.2f、n=%d。" % (p0, rate, n),
                   "得到 %.2f 元。" % val],
            exam_tip="复利题先确认期数单位；月利率要换成月数。",
            scene="存款、房贷、信用卡与长期理财")
    if pick == "bayes":
        prev = rng.choice([0.005, 0.01, 0.02, 0.05])
        sens = rng.choice([0.9, 0.95, 0.99])
        spec = rng.choice([0.9, 0.95, 0.99])
        fpr = 1 - spec
        post = prev * sens / (prev * sens + (1 - prev) * fpr)
        return _float_answer(
            round(post, 6),
            "某病患病率 %.1f%%，检测灵敏度 %.0f%%，特异度 %.0f%%。"
            "阳性后实际患病的概率约为？"
            % (prev * 100, sens * 100, spec * 100),
            "后验 = %.4f×%.2f / (%.4f×%.2f + %.4f×%.2f) = %.4f。"
            % (prev, sens, prev, sens, 1 - prev, fpr, post),
            "Bayes 模型", min(5, 3 + level // 2),
            hints=["先写 Bayes 公式。", "分母要加上健康人假阳性的部分。"],
            steps=["患病且阳性：%.4f×%.2f=%.5f。" % (prev, sens, prev * sens),
                   "健康但阳性：%.4f×%.2f=%.5f。" % (1 - prev, fpr, (1 - prev) * fpr),
                   "后验概率 %.4f。" % post],
            exam_tip="低患病率时，假阳性会把阳性后验概率拉得很低。",
            scene="医学筛查、风控与垃圾邮件过滤")
    if pick == "cooling":
        tenv = 20
        t0 = rng.choice([80, 90, 100])
        k = rng.choice([0.1, 0.2, 0.3])
        t = rng.randint(5, 30)
        val = tenv + (t0 - tenv) * math.exp(-k * t)
        return _float_answer(
            round(val, 2),
            "物体初始温度 %d°C，环境温度 20°C，冷却系数 k=%.1f。"
            "按 T(t)=T_env+(T0-T_env)e^{-kt}，t=%d 时温度约为？"
            % (t0, k, t),
            "T(%d)=20+(%d-20)e^{-%.1f×%d}=%.2f°C。" % (t, t0, k, t, val),
            "冷却模型", min(5, 2 + level // 2),
            hints=["先算初始温差 T0-T_env。", "温差按 e^{-kt} 衰减，再加回环境温度。"],
            steps=["初始温差 %d-20=%d。" % (t0, t0 - 20),
                   "衰减因子 e^{-%.1f×%d}=%.4f。" % (k, t, math.exp(-k * t)),
                   "T=20+%d×%.4f=%.2f°C。" % (t0 - 20, math.exp(-k * t), val)],
            scene="食品冷却、设备散热与建筑热工")
    if pick == "lp":
        a_cap = rng.randint(5, 15)
        b_cap = rng.randint(5, 15)
        s_cap = rng.randint(max(a_cap, b_cap), a_cap + b_cap)
        p = rng.randint(1, 10)
        q = rng.randint(1, 10)
        point, val = _lp_max(a_cap, b_cap, s_cap, p, q)
        return _int_answer(
            val,
            "在约束 x≥0，y≥0，x≤%d，y≤%d，x+y≤%d 下，"
            "最大化 %dx+%dy。最大值是？" % (a_cap, b_cap, s_cap, p, q),
            "线性规划最优值在可行域顶点取得；最优点为 (%d,%d)，"
            "最大值为 %d×%d+%d×%d=%d。"
            % (point[0], point[1], p, point[0], q, point[1], val),
            "线性规划", min(5, 3 + level // 2),
            hints=["先画出可行域，最优值在顶点。", "把每个顶点的坐标代入目标函数比较。"],
            steps=["可行域顶点包括 (0,0)、(%d,0)、(0,%d) 及约束交点。" % (a_cap, b_cap),
                   "逐点代入 %dx+%dy。" % (p, q),
                   "最优点 (%d,%d)，最大值 %d。" % (point[0], point[1], val)],
            exam_tip="线性规划先画可行域；有界闭区域上比较各顶点即可。",
            scene="生产计划、资源分配与排班")
    demand = rng.randint(1000, 10000)
    order_cost = rng.choice([50, 100, 200])
    hold_cost = rng.choice([1, 2, 5, 10])
    val = math.sqrt(2 * demand * order_cost / hold_cost)
    return _float_answer(
        round(val, 2),
        "年需求 D=%d，每次订货成本 S=%d，单位年持有成本 H=%d。"
        "经济订货批量 EOQ=√(2DS/H) 约为？" % (demand, order_cost, hold_cost),
        "EOQ=√(2×%d×%d/%d)=%.2f。" % (demand, order_cost, hold_cost, val),
        "库存模型", min(5, 2 + level // 2),
        hints=["EOQ 公式是 √(2DS/H)。", "先算根号内的 2DS/H。"],
        steps=["2DS=2×%d×%d=%d。" % (demand, order_cost, 2 * demand * order_cost),
               "除以 H=%d 后开方。" % hold_cost,
               "EOQ=%.2f。" % val],
        scene="库存管理、补货与供应链成本")


def _calc_analysis1(rng, level):
    return _calc_analysis(rng, level, ["sinx", "poly", "deriv"])


def _calc_analysis2(rng, level):
    return _calc_analysis(rng, level, ["intpow", "intexp", "geom", "taylor"])


def _calc_analysis3(rng, level):
    pick = rng.choice(["partial", "double", "green"])
    if pick == "partial":
        a, b, c = [rng.randint(-3, 3) for _ in range(3)]
        d, e = rng.randint(-5, 5), rng.randint(-5, 5)
        x0, y0 = rng.randint(-2, 2), rng.randint(-2, 2)
        which = rng.choice(["x", "y"])
        val = (2 * a * x0 + b * y0 + d) if which == "x" else (
            b * x0 + 2 * c * y0 + e)
        expr = _poly_expr([(a, "x²"), (b, "xy"), (c, "y²"), (d, "x"), (e, "y")])
        dexpr = (_poly_expr([(2 * a, "x"), (b, "y"), (d, "")])
                 if which == "x"
                 else _poly_expr([(b, "x"), (2 * c, "y"), (e, "")]))
        return _int_answer(
            val,
            "f(x,y)=%s，求 f_%s(%d,%d)。" % (expr, which, x0, y0),
            "对 %s 求偏导时把另一个变量当常数：f_%s=%s，代入得 %d。"
            % (which, which, dexpr, val),
            "偏导数", min(5, 2 + level // 2),
            hints=["对 x 求偏导时把 y 当常数，反之亦然。",
                   "先写偏导函数，再代入点的坐标。"],
            steps=["写出 f_%s 的表达式。" % which,
                   "把 (%d,%d) 代入并计算。" % (x0, y0),
                   "得到 %d。" % val],
            scene="多元函数的梯度、切平面与误差传播")
    if pick == "double":
        x1 = rng.randint(0, 2)
        x2 = x1 + rng.randint(1, 3)
        y1 = rng.randint(0, 2)
        y2 = y1 + rng.randint(1, 3)
        a, b, c = rng.randint(-3, 3), rng.randint(-3, 3), rng.randint(-3, 3)
        term_x = a * (x2 * x2 - x1 * x1) / 2 * (y2 - y1)
        term_y = b * (x2 - x1) * (y2 * y2 - y1 * y1) / 2
        term_c = c * (x2 - x1) * (y2 - y1)
        val = term_x + term_y + term_c
        expr = _poly_expr([(a, "x"), (b, "y"), (c, "")])
        return _float_answer(
            round(val, 6),
            "计算 ∬_R (%s) dA，其中 R=[%d,%d]×[%d,%d]。"
            % (expr, x1, x2, y1, y2),
            "利用线性性拆成三项：∬ax dA + ∬by dA + ∬c dA，"
            "分别积分后相加得 %.4f。" % val,
            "重积分", min(5, 3 + level // 2),
            hints=["矩形区域上可以分别对 x、y 积分。",
                   "利用积分的线性性，把多项式拆成三项。"],
            steps=["∬ ax dA = %.4f。" % term_x,
                   "∬ by dA = %.4f。" % term_y,
                   "∬ c dA = %.4f。" % term_c,
                   "三项相加得 %.4f。" % val],
            scene="面积、平均值、总质量与概率密度积分")
    a = rng.randint(1, 5)
    val = 2 * a * a
    return _int_answer(
        val,
        "沿正方形 [0,%d]×[0,%d] 的边界逆时针方向，计算 ∮ -y dx + x dy。"
        % (a, a),
        "Green 公式：Q_x-P_y=1-(-1)=2，区域面积 %d²=%d，积分为 2×%d=%d。"
        % (a, a * a, a * a, val),
        "Green 公式", min(5, 3 + level // 2),
        hints=["先确认曲线方向与 P、Q。", "Green 公式把线积分换成二重积分。"],
        steps=["P=-y，Q=x，Q_x-P_y=2。",
               "区域是边长 %d 的正方形，面积 %d。" % (a, a * a),
               "线积分 = 2×面积 = %d。" % val],
        scene="环量、旋度与平面流体")


def _calc_poly1(rng, level):
    a, b, c = [rng.randint(-4, 4) for _ in range(3)]
    d = rng.randint(-4, 4)
    val = d ** 3 + a * d * d + b * d + c
    expr = _poly_expr([(1, "x³"), (a, "x²"), (b, "x"), (c, "")])
    return _int_answer(
        val,
        "f(x)=%s，求 f(x) 除以 x%+d 的余数。" % (expr, -d),
        "余数定理：余数 = f(%d) = %d³%+d×%d²%+d×%d%+d = %d。"
        % (d, d, a, d, b, d, c, val),
        "多项式", min(5, 2 + level // 2),
        hints=["除以 x-d 的余数就是 f(d)。", "把 d 代入 f(x) 计算。"],
        steps=["写出余数定理：r=f(d)。",
               "代入 d=%d。" % d,
               "得到余数 %d。" % val],
        exam_tip="多项式除法先用余数定理，能避免长除法算错。",
        scene="多项式求值、编码与校验")


def _calc_quadform(rng, level):
    a, c = rng.randint(1, 5), rng.randint(1, 5)
    b = rng.randint(-4, 4)
    val = a * c - b * b
    return _int_answer(
        val,
        "二次型 f(x,y)=%dx²%+dxy%+dy² 的矩阵行列式是？" % (a, 2 * b, c),
        "二次型的对称矩阵是 [[%d,%d],[%d,%d]]，行列式 = %d×%d-%d² = %d。"
        % (a, b, b, c, a, c, b, val),
        "二次型", min(5, 3 + level // 2),
        hints=["先写成 1/2 x^T A x 的对称矩阵。", "交叉项系数 2b 对应矩阵的 b。"],
        steps=["矩阵 A=[[%d,%d],[%d,%d]]。" % (a, b, b, c),
               "det(A)=%d×%d-%d²。" % (a, c, b),
               "得到 %d；若 a>0 且 det>0 则正定。" % val],
        exam_tip="二次型正定先看顺序主子式；二阶只需 a>0 且 ac-b²>0。",
        scene="最优化中的凸性、稳定性与数据降维")


def _calc_transform(rng, level):
    a, b, c, d = [rng.randint(-4, 4) for _ in range(4)]
    val = a * d - b * c
    return _int_answer(
        val,
        "线性变换 T(x,y)=(%dx%+dy, %dx%+dy) 的矩阵行列式是？"
        % (a, b, c, d),
        "T 的矩阵为 [[%d,%d],[%d,%d]]，行列式 = %d×%d-%d×%d = %d。"
        % (a, b, c, d, a, d, b, c, val),
        "线性变换", min(5, 2 + level // 2),
        hints=["把 T(1,0) 和 T(0,1) 作为矩阵的两列。", "二阶行列式 ad-bc。"],
        steps=["矩阵 A=[[%d,%d],[%d,%d]]。" % (a, b, c, d),
               "det(A)=%d×%d-%d×%d。" % (a, d, b, c),
               "得到 %d；det≠0 时变换可逆。" % val],
        scene="坐标变换、图像缩放旋转与机器人运动学")


def _calc_algebra1(rng, level):
    pick = rng.choice(["det2", "det3", "rank", "solve", "poly"])
    if pick == "poly":
        return _calc_poly1(rng, level)
    return _calc_algebra(rng, level, [pick])


def _calc_algebra2(rng, level):
    pick = rng.choice(["eigen", "trace", "quadform", "transform"])
    if pick == "quadform":
        return _calc_quadform(rng, level)
    if pick == "transform":
        return _calc_transform(rng, level)
    return _calc_algebra(rng, level, [pick])


def _calc_discrete(rng, level):
    """离散数学计算：组合数、排列数、完全图边数、子集数、树叶子。"""
    pick = rng.choice(["comb", "perm", "edges", "subset", "leaves"])
    if pick == "comb":
        n = rng.randint(5, 9)
        k = rng.randint(2, min(4, n - 1))
        val = math.comb(n, k)
        return _int_answer(val, "C(%d,%d) = ?" % (n, k),
                           "组合数 C(n,k)=n!/(k!(n-k)!)，代入得 %d。" % val,
                           "组合计数", min(5, 1 + level // 2))
    if pick == "perm":
        n = rng.randint(4, 7)
        k = rng.randint(2, 3)
        val = math.perm(n, k)
        return _int_answer(val, "P(%d,%d) = ?" % (n, k),
                           "排列数 P(n,k)=n!/(n-k)!，代入得 %d。" % val,
                           "组合计数", min(5, 1 + level // 2))
    if pick == "edges":
        n = rng.randint(4, 8)
        val = n * (n - 1) // 2
        return _int_answer(val, "完全图 K_%d 有多少条边？" % n,
                           "每对顶点一条边，共 C(%d,2)=%d 条。" % (n, val),
                           "图论基础", min(5, 1 + level // 2))
    if pick == "subset":
        n = rng.randint(3, 8)
        val = 2 ** n
        return _int_answer(val, "一个 %d 元集合有多少个子集？" % n,
                           "每个元素选或不选，共 2^%d=%d 个。" % (n, val),
                           "集合与关系", min(5, 1 + level // 2))
    n = rng.randint(4, 9)
    return _int_answer(2, "一棵有 %d 个顶点的树至少有几个叶子？" % n,
                       "n≥2 的树至少有 2 个叶子，路径图恰好达到 2。",
                       "树", min(5, 2 + level // 2))


CALC_GENERATORS = {
    "analysis": _calc_analysis,
    "algebra": _calc_algebra,
    "number": _calc_number,
    "abstract": _calc_abstract,
    "probability": _calc_probability,
    "statistics": _calc_statistics,
    "ode": _calc_ode,
    "complex": _calc_complex,
    "geometry": _calc_geometry,
    "numerical": _calc_numerical,
    "modeling": _calc_modeling,
    "discrete": _calc_discrete,
}

# 选课粒度到生成器的映射
COURSE_GENERATORS = {
    "analysis1": _calc_analysis1,
    "analysis2": _calc_analysis2,
    "analysis3": _calc_analysis3,
    "algebra1": _calc_algebra1,
    "algebra2": _calc_algebra2,
    "number": _calc_number,
    "abstract": _calc_abstract,
    "probability": _calc_probability,
    "statistics": _calc_statistics,
    "ode": _calc_ode,
    "complex": _calc_complex,
    "geometry": _calc_geometry,
    "numerical": _calc_numerical,
    "modeling": _calc_modeling,
    "discrete": _calc_discrete,
}


def _fmt(v):
    if isinstance(v, bool):
        return "正确" if v else "错误"
    if isinstance(v, int) or (isinstance(v, float) and abs(v - round(v)) < 1e-9):
        return str(int(round(v)))
    return ("%.6f" % float(v)).rstrip("0").rstrip(".")


def _public_question(q):
    out = {"id": q["id"], "subject": q["subject"],
           "subject_name": SUBJECT_NAMES[q["subject"]],
           "course": q.get("course"),
           "course_name": COURSE_NAMES.get(q.get("course"), q.get("course", "")),
           "kind": q["kind"], "prompt": q["prompt"],
           "difficulty": q["difficulty"], "skill": q.get("skill", ""),
           "hints": list(q.get("hints") or []),
           "scene": q.get("scene", "")}
    if q["kind"] == "calc":
        out["unit"] = q.get("unit", "")
        out["answer_type"] = "number"
    if q["kind"] == "counter":
        out["options"] = q["options"]
    if q["kind"] == "tf":
        out["options"] = [{"id": "true", "text": "正确"},
                          {"id": "false", "text": "错误"}]
    return out


# ------------------------------------------------------------
# 对局仓库
# ------------------------------------------------------------

_ROUNDS = {}
_PAPERS = {}
_LOCK = threading.RLock()


def _gc_locked(now=None):
    now = now or time.time()
    for rid in [r for r, v in _ROUNDS.items() if now - v["created"] > ROUND_TTL_SEC]:
        _ROUNDS.pop(rid, None)


def _expand_subject(s):
    if s == "analysis":
        return ["analysis1", "analysis2", "analysis3"]
    if s == "algebra":
        return ["algebra1", "algebra2"]
    return [s] if s in COURSE_NAMES else []


def _normalize_courses(courses=None, subjects=None):
    out = []
    if isinstance(courses, str):
        courses = [courses]
    if isinstance(courses, (list, tuple, set)):
        for c in courses:
            if isinstance(c, str) and c in COURSE_NAMES and c not in out:
                out.append(c)
    if not out and subjects is not None:
        if isinstance(subjects, str):
            subjects = [subjects]
        if isinstance(subjects, (list, tuple, set)):
            for s in subjects:
                for c in _expand_subject(s):
                    if c not in out:
                        out.append(c)
    return out or list(COURSE_ORDER)


def _normalize_subjects(subjects):
    """兼容旧调用：大学科自动展开为学期课程。"""
    return _normalize_courses(subjects=subjects)


def _concept_course(q):
    return q.get("course") or _course_of(q.get("subject"), q.get("skill", ""))


def _pick_concept(rng, courses, kind, level, used):
    """只在给定选课粒度里抽概念题；单科没有该题型时退化到同批其它课程。"""
    courses = courses or list(COURSE_ORDER)

    def pool_of(course_list, respect_used=True, respect_level=True):
        return [q for q in CONCEPTS
                if _concept_course(q) in course_list and q["kind"] == kind
                and (not respect_used or q["prompt"] not in used)
                and (not respect_level or abs(q["difficulty"] - level) <= 2)]

    # 1) 先试指定课程 + 难度窗口；2) 再放宽难度；3) 再放宽到所选其它课程
    for course in [courses[0]] + [c for c in courses if c != courses[0]]:
        for check_level in (True, False):
            pool = pool_of([course], True, check_level)
            if pool:
                q = dict(rng.choice(pool))
                break
        else:
            continue
        break
    else:
        pool = pool_of(courses, True, False)
        if not pool:
            pool = pool_of(courses, False, False)
        if not pool:
            pool = [q for q in CONCEPTS if q["kind"] == kind]
        q = dict(rng.choice(pool))
    if kind == "counter":
        q = {**q, "options": [dict(x) for x in q["options"]]}
    return _decorate(q)


def _build_questions(rng, level, courses):
    kinds = ["calc", "calc", "calc", "tf", "counter"]
    rng.shuffle(kinds)                 # 题型顺序打散，但数量固定：3 计算 + 1 判断 + 1 反例
    courses = list(courses) or list(COURSE_ORDER)
    rng.shuffle(courses)
    questions, used = [], set()
    for i, kind in enumerate(kinds):
        course = courses[i % len(courses)]
        if kind == "calc":
            gen = COURSE_GENERATORS[course]
            q = gen(rng, level)
            # 计算题偶尔与上一个重复：换一次生成器
            for _ in range(3):
                if q["prompt"] not in used:
                    break
                q = gen(rng, level)
            subject = COURSE_PARENT[course]
        else:
            # 概念题优先用当前课程，其次在同批所选课程里找该题型
            order = [course] + [c for c in courses if c != course]
            q = _pick_concept(rng, order, kind, level, used)
            subject = q["subject"]
            course = q["course"]
        q["id"] = "q%d" % (i + 1)
        q["subject"] = subject
        q["course"] = course
        q = _decorate(q)
        used.add(q["prompt"])
        questions.append(q)
    return questions


def new_round(level=3, ai_count=MAX_AI, seed=None, subjects=None, courses=None):
    try:
        level = int(level or 3)
    except (TypeError, ValueError):
        level = 3
    level = max(1, min(5, level))
    try:
        ai_count = int(ai_count)
    except (TypeError, ValueError):
        ai_count = MAX_AI
    selected = _normalize_courses(courses, subjects)
    rng = random.Random(seed) if seed is not None else random.Random()
    questions = _build_questions(rng, level, selected)
    rid = uuid.uuid4().hex[:12]
    players = [{"id": HUMAN_ID, "name": "你", "kind": "human"}]
    players += [dict(p, kind="ai")
                for p in AI_PLAYERS[:max(0, min(ai_count, MAX_AI))]]
    with _LOCK:
        _gc_locked()
        _ROUNDS[rid] = {
            "id": rid, "created": time.time(), "level": level,
            "questions": questions, "players": players,
            "answers": {p["id"]: {} for p in players},
            "finished": False, "rng_seed": seed, "courses": selected,
        }
        return _ROUNDS[rid]


def public_round(r):
    courses = r.get("courses") or list(COURSE_ORDER)
    parents = []
    for c in courses:
        p = COURSE_PARENT[c]
        if p not in parents:
            parents.append(p)
    return {"id": r["id"], "level": r["level"],
            "questions": [_public_question(q) for q in r["questions"]],
            "players": [{"id": p["id"], "name": p["name"], "kind": p["kind"]}
                        for p in r["players"]],
            "courses": [{"id": c, "name": COURSE_NAMES[c],
                         "subject": COURSE_PARENT[c]} for c in courses],
            "subjects": [{"id": s, "name": SUBJECT_NAMES[s]} for s in parents],
            "count": len(r["questions"])}


def _live_locked(rid):
    _gc_locked()
    r = _ROUNDS.get(rid)
    if not r:
        return None, "对局已过期，请开新的一局"
    if r["finished"]:
        return None, "这一局已经结束了"
    return r, ""


def _normalize(value, q):
    if value is None or value == "":
        return None
    if q["kind"] == "calc":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None
    if q["kind"] == "tf":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "1", "正确", "对"):
            return True
        if s in ("false", "0", "错误", "错"):
            return False
        return None
    s = str(value).strip()
    return s if any(o["id"] == s for o in q.get("options") or []) else None


def _is_exact_int(value):
    return abs(float(value) - round(float(value))) < 1e-9


def _display_answer(value, q):
    """把规范化后的答案转成观测里可读的文本（判分逻辑不放这里）。"""
    if value is None:
        return "未作答"
    if q.get("kind") == "tf":
        return "正确" if value else "错误"
    if q.get("kind") == "counter":
        opts = {o["id"]: o["text"] for o in q.get("options") or []}
        return str(opts.get(value, value))
    return str(value)


def _is_correct(value, q):
    if value is None:
        return False
    if q["kind"] == "calc":
        ans = float(q["answer"])
        # 整数答案（gcd / φ / 秩 / 子群数 …）必须精确；小数答案允许 1% 误差
        if _is_exact_int(ans):
            return abs(value - ans) <= 1e-6
        return abs(value - ans) <= max(1e-9, abs(ans) * 0.01)
    return value == q["answer"]


def _score(value, q, elapsed_ms):
    if value is None:
        return 0
    correct = _is_correct(value, q)
    if not correct:
        # 整数答案没有「接近」这回事；小数计算题按误差给部分分
        if q["kind"] == "calc" and not _is_exact_int(q["answer"]):
            ans = float(q["answer"])
            err = abs(value - ans) / max(1e-9, abs(ans))
            return max(0, round(100 * (1 - min(1.0, err / 0.25))))
        return 0
    # 速度分只奖励答对的题
    return 100 + max(0, 30 - int(max(0, elapsed_ms) / 1000))


def _ai_value(ai, q, level, rng):
    acc = min(0.95, ai["base"] + 0.05 * level
              + (ai["tf_bonus"] if q["kind"] != "calc" else ai["calc_bonus"]))
    correct = rng.random() < acc
    if q["kind"] == "calc":
        ans = float(q["answer"])
        if abs(ans) < 1e-9:
            # 答案为 0 时无法按比例加噪
            return 0.0 if correct else float(rng.choice([-1, 1]) * rng.randint(1, 5))
        if _is_exact_int(ans):
            # 整数题模拟「算对了 / 算错了」：算对就给精确值，算错至少偏 1
            if correct:
                return float(round(ans))
            delta = max(1.0, abs(ans) * 0.03)
            return ans + rng.choice([-1, 1]) * delta * rng.uniform(1.0, 2.0)
        if correct:
            # 落在 1% 容差内，模拟「算对了」
            return ans * (1 + rng.uniform(-0.008, 0.008))
        # 算错时至少偏离 3%，确保不会误判为正确
        sign = rng.choice([-1, 1])
        return ans * (1 + sign * rng.uniform(0.03, 0.2))
    if q["kind"] == "tf":
        return q["answer"] if correct else (not q["answer"])
    opts = [o["id"] for o in q.get("options") or []]
    if correct:
        return q["answer"]
    wrong = [x for x in opts if x != q["answer"]]
    return rng.choice(wrong) if wrong else q["answer"]


def _ai_reason(ai, q, value, correct):
    hint = q.get("ai_hint") or q.get("explain") or ""
    if q["kind"] == "calc":
        return ("%s 我先估量级，再算到 %s 附近。" % (ai["style"], _fmt(value))
                if correct else "%s 我算到了 %s，但和正确答案差了一点。" % (ai["style"], _fmt(value)))
    if q["kind"] == "tf":
        if not correct:
            # 答错时不要把正确解析塞在错误判断后面：那会自相矛盾，也会提前泄露思路
            return "%s 我判断这个命题为%s，这一步想当然了。" % (
                ai["style"], "真" if value else "假")
        return "%s 我判断这个命题为%s。%s" % (
            ai["style"], "真" if value else "假", hint)
    opts = {o["id"]: o["text"] for o in q.get("options") or []}
    if not correct:
        return "%s 我选「%s」，看来被干扰项骗了。" % (ai["style"], opts.get(value, value))
    return "%s 我选「%s」。%s" % (ai["style"], opts.get(value, value), hint)


def answer(rid, qid, value, elapsed_ms=0, player_id=HUMAN_ID):
    with _LOCK:
        r, err = _live_locked(rid)
        if err:
            raise MathError(err)
        q = next((x for x in r["questions"] if x["id"] == qid), None)
        if not q:
            raise MathError("没有这道题")
        if qid in r["answers"].get(player_id, {}):
            raise MathError("这道题已经答过了")
        try:
            elapsed = int(elapsed_ms or 0)
        except (TypeError, ValueError):
            elapsed = 0
        v = _normalize(value, q)
        correct = _is_correct(v, q)
        score = _score(v, q, elapsed)
        r["answers"].setdefault(player_id, {})[qid] = {
            "value": v, "correct": correct, "score": score, "elapsed_ms": elapsed,
        }

        # AI 同步作答（纯本地计算，毫秒级）
        base = r.get("rng_seed")
        if base is None:
            base = time.time_ns()
        rng = random.Random((int(base) * 1000003) ^ zlib.crc32(qid.encode("utf-8")))
        ai_rows = []
        for ai in r["players"]:
            if ai["kind"] != "ai":
                continue
            if qid in r["answers"].get(ai["id"], {}):
                continue
            av = _ai_value(ai, q, r["level"], rng)
            ac = _is_correct(av, q)
            ms = rng.randint(*ai["speed"]) * 1000
            ascore = _score(av, q, ms)
            reason = _ai_reason(ai, q, av, ac)
            r["answers"].setdefault(ai["id"], {})[qid] = {
                "value": av, "correct": ac, "score": ascore, "reason": reason,
            }
            ai_rows.append({
                "player_id": ai["id"], "name": ai["name"], "value": av,
                "correct": ac, "score": ascore, "reason": reason,
            })

        answer_display = q["answer"]
        if q["kind"] == "counter":
            opts = {o["id"]: o["text"] for o in q.get("options") or []}
            answer_display = opts.get(q["answer"], q["answer"])
        out = {
            "correct": correct, "score": score,
            "answer": answer_display, "answer_value": q["answer"],
            "explain": q.get("explain", ""), "skill": q.get("skill", ""),
            "course": q.get("course"),
            "course_name": COURSE_NAMES.get(q.get("course"), ""),
            "steps": list(q.get("steps") or []),
            "exam_tip": q.get("exam_tip", ""),
            "scene": q.get("scene", ""),
            "ai": ai_rows,
            "answered": len(r["answers"][player_id]),
            "total": len(r["questions"]),
        }
        user_display = _display_answer(v, q)
        course_name = COURSE_NAMES.get(q.get("course"), "")

    # 观测写在锁外：数据库 I/O 不占对局锁，也不能影响判分结果
    try:
        from app import observe
        observe.record(
            source="math_arena", subject=course_name, topic=q.get("skill", ""),
            value={"correct": bool(correct), "score": score, "elapsed_ms": elapsed},
            evidence="题目：%s\n我的答案：%s\n正确答案：%s"
                     % (q.get("prompt", ""), user_display, answer_display))
    except Exception as e:                       # noqa: BLE001
        print("[数学] 记录观测失败：%s" % e)
    return out


def finish(rid, player_id=HUMAN_ID):
    with _LOCK:
        _gc_locked()
        r = _ROUNDS.get(rid)
        if not r:
            raise MathError("对局已过期，请开新的一局")
        total = len(r["questions"])
        mine = r["answers"].get(player_id, {})
        if len(mine) < total:
            raise MathError("还有题目没答完")
        r["finished"] = True
        rows = []
        for p in r["players"]:
            ans = r["answers"].get(p["id"], {})
            rows.append({
                "player_id": p["id"], "name": p["name"], "kind": p["kind"],
                "score": sum(x["score"] for x in ans.values()),
                "correct": sum(1 for x in ans.values() if x["correct"]),
                "answered": len(ans),
            })
        rows.sort(key=lambda x: (-x["score"], -x["correct"]))
        for i, row in enumerate(rows):
            row["rank"] = i + 1
        me = next(x for x in rows if x["player_id"] == player_id)
        ability_rows = []
        for q in r["questions"]:
            a = mine.get(q["id"]) or {}
            ability_rows.append({
                "skill": q.get("skill"),
                "correct": bool(a.get("correct")),
                "score": a.get("score") or 0,
                "elapsed_ms": a.get("elapsed_ms") or 0,
            })
        agg = math_abilities.aggregate(ability_rows)
        return {"score": me["score"], "correct": me["correct"], "total": total,
                "rank": me["rank"], "players": rows,
                "by_skill": agg["by_skill"], "by_ability": agg["by_ability"]}


def reset_store():
    with _LOCK:
        _ROUNDS.clear()
        _PAPERS.clear()


# ------------------------------------------------------------
# 期末模拟卷
# ------------------------------------------------------------

PAPER_TTL_SEC = 6 * 3600
PAPER_MIN = 5
PAPER_MAX = 30


def _gc_papers_locked(now=None):
    now = now or time.time()
    for pid in [p for p, v in _PAPERS.items()
                if now - v.get("created", 0) > PAPER_TTL_SEC]:
        _PAPERS.pop(pid, None)


def _paper_mix(count):
    """返回 (计算, 判断, 反例) 题量；概念题不足时由调用方补计算题。"""
    try:
        count = int(count or 15)
    except (TypeError, ValueError):
        count = 15
    count = max(PAPER_MIN, min(PAPER_MAX, count))
    if count <= 10:
        tf, counter = 2, 1
    elif count <= 15:
        tf, counter = 3, 2
    else:
        tf, counter = 4, 3
    tf = min(tf, count)
    counter = min(counter, max(0, count - tf))
    return max(0, count - tf - counter), tf, counter


def paper_meta():
    """课程与知识点列表，供前端期末设置使用。"""
    skills = {}
    for q in CONCEPTS:
        course = _concept_course(q)
        skill = str(q.get("skill") or "").strip()
        if skill:
            skills.setdefault(course, set()).add(skill)
    courses = []
    for c in COURSE_ORDER:
        courses.append({
            "id": c, "name": COURSE_NAMES[c],
            "skills": sorted(skills.get(c, [])),
        })
    return {"courses": courses, "default_count": 15, "default_duration": 90}


def ability_meta():
    """8 维过程能力 + 知识点到能力的主映射。"""
    return math_abilities.meta()


def _paper_public(q):
    out = _public_question(q)
    out.pop("hints", None)          # 考试中不通过接口暴露提示
    return out


def _paper_concepts(course, kind, need, skill, used, rng):
    pool = [q for q in CONCEPTS
            if _concept_course(q) == course and q.get("kind") == kind
            and q.get("prompt") not in used]
    if skill:
        subset = [q for q in pool if q.get("skill") == skill]
        if subset:
            pool = subset
    rng.shuffle(pool)
    out = []
    for q in pool[:max(0, need)]:
        item = copy.deepcopy(q)
        if kind == "counter":
            item["options"] = [dict(x) for x in item.get("options") or []]
        out.append(item)
    return out


def _paper_calc(course, need, skill, level, used, rng):
    if need <= 0:
        return []
    gen = COURSE_GENERATORS[course]
    picked, fallback = [], []
    seen = set(used)
    for _ in range(max(80, need * 50)):
        q = gen(rng, level)
        if not isinstance(q, dict) or q.get("prompt") in seen:
            continue
        _decorate(q)
        if q.get("prompt") in seen:
            continue
        if skill and q.get("skill") != skill:
            fallback.append(q)
            continue
        picked.append(q)
        seen.add(q.get("prompt"))
        if len(picked) >= need:
            break
    if len(picked) < need and skill:
        for q in fallback:
            if q.get("prompt") in seen:
                continue
            picked.append(q)
            seen.add(q.get("prompt"))
            if len(picked) >= need:
                break
    return picked


def new_paper(course, count=15, duration=90, level=3, skill="", seed=None):
    """生成一套只属于单门课程的期末模拟卷（不返回答案与提示）。"""
    with _LOCK:
        _gc_papers_locked()
        course = str(course or "").strip()
        if course not in COURSE_NAMES:
            raise MathError("请选择一门课程")
        try:
            level = max(1, min(5, int(level or 3)))
        except (TypeError, ValueError):
            level = 3
        try:
            duration = max(10, min(240, int(duration or 90)))
        except (TypeError, ValueError):
            duration = 90
        skill = str(skill or "").strip()
        calc_need, tf_need, counter_need = _paper_mix(count)
        rng = random.Random(seed) if seed is not None else random.Random()
        used, warnings = set(), []

        tf_q = _paper_concepts(course, "tf", tf_need, skill, used, rng)
        for q in tf_q:
            used.add(q.get("prompt"))
        counter_q = _paper_concepts(course, "counter", counter_need, skill, used, rng)
        for q in counter_q:
            used.add(q.get("prompt"))
        if len(tf_q) < tf_need:
            warnings.append("判断题不足，已用计算题补足")
        if len(counter_q) < counter_need:
            warnings.append("反例题不足，已用计算题补足")

        calc_need += (tf_need - len(tf_q)) + (counter_need - len(counter_q))
        calc_q = _paper_calc(course, calc_need, skill, level, used, rng)
        if skill and any(q.get("skill") != skill for q in calc_q):
            warnings.append("该知识点的计算题不足，已混入同课程其他知识点")
        if len(calc_q) < calc_need:
            warnings.append("可生成的题目不足，本卷题量已自动减少")

        questions = []
        for q in calc_q + tf_q + counter_q:
            q = _decorate(q)
            q["subject"] = COURSE_PARENT[course]
            q["course"] = course
            questions.append(q)
        rng.shuffle(questions)
        for i, q in enumerate(questions):
            q["id"] = "e%d" % (i + 1)

        if len(questions) < PAPER_MIN:
            raise MathError("这门课当前题目不足，换一门课试试")

        pid = uuid.uuid4().hex[:12]
        paper = {
            "id": pid, "course": course, "created": time.time(),
            "duration": duration, "level": level, "skill": skill,
            "questions": questions, "graded": False, "result": None,
        }
        _PAPERS[pid] = paper
        return {
            "paper_id": pid, "course": course,
            "course_name": COURSE_NAMES[course],
            "duration": duration, "level": level, "skill": skill,
            "count": len(questions), "warnings": warnings,
            "questions": [_paper_public(q) for q in questions],
        }


def grade_paper(pid, answers=None, time_ms=0):
    """交卷后批量判分；考试中不返回任何答案。"""
    with _LOCK:
        _gc_papers_locked()
        paper = _PAPERS.get(str(pid or ""))
        if not paper:
            raise MathError("试卷已过期，请重新开一套")
        by_id = {}
        if isinstance(answers, dict):
            by_id = answers
        elif isinstance(answers, (list, tuple)):
            for item in answers:
                if isinstance(item, dict) and item.get("qid") is not None:
                    by_id[str(item.get("qid"))] = item
        rows, skill_stats = [], {}
        total_score = 0
        correct_count = 0
        for q in paper["questions"]:
            item = by_id.get(q["id"])
            raw = item.get("value") if isinstance(item, dict) else item
            elapsed = item.get("elapsed_ms", 0) if isinstance(item, dict) else 0
            try:
                elapsed = max(0, int(elapsed or 0))
            except (TypeError, ValueError):
                elapsed = 0
            v = _normalize(raw, q)
            correct = bool(v is not None and _is_correct(v, q))
            score = min(100, _score(v, q, elapsed)) if v is not None else 0
            total_score += score
            if correct:
                correct_count += 1
            answer_display = q.get("answer")
            if q["kind"] == "counter":
                opts = {o["id"]: o["text"] for o in q.get("options") or []}
                answer_display = opts.get(q.get("answer"), q.get("answer"))
            elif q["kind"] == "tf":
                answer_display = "正确" if q.get("answer") else "错误"
            skill = str(q.get("skill") or "未标注")
            st = skill_stats.setdefault(skill, {
                "skill": skill, "attempts": 0, "correct": 0,
                "score": 0, "time_ms": 0,
            })
            st["attempts"] += 1
            st["correct"] += 1 if correct else 0
            st["score"] += score
            st["time_ms"] += elapsed
            explain = q.get("explain", "")
            explain_key = str(explain).strip(" \t\r\n。；;.")
            steps = [s for s in (q.get("steps") or [])
                     if s and str(s).strip(" \t\r\n。；;.") not in explain_key]
            rows.append({
                "qid": q["id"], "prompt": q.get("prompt", ""),
                "kind": q.get("kind"), "options": q.get("options") or [],
                "skill": skill, "difficulty": q.get("difficulty", 2),
                "course": q.get("course"), "course_name": COURSE_NAMES.get(q.get("course"), ""),
                "value": v, "answer": answer_display,
                "answer_value": q.get("answer"),
                "correct": correct, "score": score, "time_ms": elapsed,
                "explain": explain,
                "steps": steps,
                "exam_tip": q.get("exam_tip", ""),
                "scene": q.get("scene", ""),
            })
        for st in skill_stats.values():
            st["accuracy"] = round(100.0 * st["correct"] / max(1, st["attempts"]), 1)
        agg = math_abilities.aggregate([{
            "skill": row.get("skill"), "correct": row.get("correct"),
            "score": row.get("score") or 0, "elapsed_ms": row.get("time_ms") or 0,
        } for row in rows])
        result = {
            "score": round(total_score / max(1, len(rows)), 1),
            "correct": correct_count, "total": len(rows),
            "accuracy": round(100.0 * correct_count / max(1, len(rows)), 1),
            "time_ms": max(0, int(time_ms or 0)),
            "by_skill": list(skill_stats.values()),
            "by_ability": agg["by_ability"],
            "questions": rows,
        }
        paper["graded"] = True
        paper["result"] = result

    # 交卷后一次性落观测；批量写，失败不影响判分
    try:
        from app import observe
        observe.record_many([
            {"source": "math_paper",
             "subject": row.get("course_name") or "",
             "topic": row.get("skill") or "",
             "value": {"correct": bool(row.get("correct")),
                       "score": row.get("score") or 0,
                       "elapsed_ms": row.get("time_ms") or 0},
             "evidence": "题目：%s\n我的答案：%s\n正确答案：%s"
                         % (row.get("prompt", ""),
                            _display_answer(row.get("value"), row),
                            row.get("answer", ""))}
            for row in rows
        ])
    except Exception as e:                       # noqa: BLE001
        print("[数学] 记录观测失败：%s" % e)
    return result

from app.math.proofs import PROOFS, proof_meta, grade_proof  # noqa: E402,F401
