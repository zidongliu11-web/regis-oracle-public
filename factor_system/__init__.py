"""工业级自动因子挖掘与回测系统（8GB 单机诚实适配）。

模块一览：
  config     全局配置与阈值
  data       PIT 面板加载（日期×代码矩阵）
  operators  算子库（时序/横截面/行业中性化）
  templates  A-I 因子模板库 + 参数网格
  metrics    IC / IC_IR / 分层利差 / p 值
  validate   L0-L3 多级检验管道
  search     组合/参数网格/遗传 三种搜索
  registry   SQLite 因子注册中心 + 生命周期
  backtest   事件驱动 t+1 回测（含全部成本）
  pipeline   端到端编排：mine→validate→register→backtest
  status     Dual-zone 状态文件（mining.json / backtest.json）
  run        CLI 入口（--mode mine|backtest|full）
"""

__version__ = "0.1.0"
