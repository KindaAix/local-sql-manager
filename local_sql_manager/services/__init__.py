"""服务层：所有 DDL 与 DML 的唯一入口。

导入本包即完成撤销处理器的注册，主程序与测试都应从这里进入服务层：

1. `undo`：先导入它，注册内置的行级/表级处理器；其余模块都在此基础上扩展。
2. `import_excel`：提供导入流程（不依赖 undo），先加载可确保导入 payload
   的格式定义就绪。
3. `fields` / `merge`：模块导入时注册 create_field / rename_field /
   drop_field / set_field_type 与 merge 的撤销处理器。
4. `undo_handlers`：最后导入。它依赖 undo 的公开助手与导入 payload 结构，
   注册 import 的撤销处理器。

新增带撤销能力的服务时，必须在本文件登记；只 import 子模块而不经过本包，
界面点撤销会报「不支持撤销的操作类型」。
"""

from . import undo  # noqa: F401  内置撤销处理器
from . import import_excel  # noqa: F401  导入流程与 payload 结构
from . import fields  # noqa: F401  注册字段级撤销处理器
from . import merge  # noqa: F401  注册合并撤销处理器
from . import undo_handlers  # noqa: F401  注册导入撤销处理器
