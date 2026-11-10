"""服务层：所有 DDL 与 DML 的唯一入口。

导入本包即完成撤销处理器的注册：

1. `undo`：先注册内置的行级/表级处理器，其余模块都在它之上扩展。
2. `fields` / `merge`：模块导入时注册 create_field / rename_field /
   drop_field / set_field_type 与 merge 的撤销处理器。
3. `undo_handlers`：最后导入，注册 import 的撤销处理器。

新增带撤销能力的服务时，必须保证该模块在包内被导入一次（可以加在本文件，
也可以由其它已加载的模块导入）；只在别处按需 import 子模块而不经过本包，
主程序里就可能缺处理器，界面点撤销会报「不支持撤销的操作类型」。
"""

from . import undo  # noqa: F401  内置撤销处理器
from . import fields  # noqa: F401  注册字段级撤销处理器
from . import merge  # noqa: F401  注册合并撤销处理器
from . import undo_handlers  # noqa: F401  注册导入撤销处理器
