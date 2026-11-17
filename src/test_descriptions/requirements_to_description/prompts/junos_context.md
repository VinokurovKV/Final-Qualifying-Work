Проверенный контекст Juniper EX4650. Проверено по официальным материалам 2026-11-16.

- Feature Explorer подтверждает IPv4 GRE encapsulation и de-encapsulation для
  EX4650-48Y начиная с Junos OS 18.3R1. Это не подтверждение версии установленного
  Junos, конкретного XML на данном устройстве или всех проверок RFC 2784.
  https://apps.juniper.net/feature-explorer/feature/3185
  https://apps.juniper.net/feature-explorer/feature/3184
- Junos XML использует корень configuration. Идентификатор именованного объекта
  обычно представлен дочерним name перед остальными настройками. Флаги — пустые
  элементы. Это правило отображения CLI, не полная XSD.
  https://www.juniper.net/documentation/us/en/software/junos/junos-xml-protocol/topics/concept/junos-xml-mapping-configuration-statements-to-junos-xml-tag-elements.html
- IPv4-адрес интерфейса с маской записывается целиком в address/name:
  <address><name>${INTERFACE_CIDR}</name></address>, без отдельного prefix-length.
- NETCONF edit-config переносит configuration внутри config. Здесь создаётся только
  configuration-фрагмент с намерением merge. RPC, validate и commit не выполняются.
  https://www.juniper.net/documentation/us/en/software/junos/netconf/topics/task/netconf-configuration-elements-changing.html
  https://www.juniper.net/documentation/us/en/software/junos/netconf/topics/task/netconf-configuration-elements-merging.html
- Руководство GRE для коммутаторов описывает gr-0/0/0, unit, tunnel source/destination
  и маршрут через туннель. Удалённая точка должна быть достижима через underlay.
  Полный путь: interfaces/interface/unit/tunnel. Внутри source и destination
  находятся непосредственно IP-значения, без вложенного name.
  Отдельно обсуждается включение tunnel-services через chassis; точный режим
  выделения ресурсов на данном EX4650 и релизе здесь не подтверждён. Не генерируй
  догадки о chassis: отметь неподтверждённую предпосылку в XML-комментарии.
  Руководство не заявляет полную поддержку RFC 2784 и перечисляет ограничения EX,
  включая GRE keepalive, keys и sequence numbers. Не добавляй их в конфигурацию.
  https://www.juniper.net/documentation/us/en/software/junos/interfaces-ethernet-switches/topics/topic-map/switches-interface-gre.html
  https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/ref/statement/tunnel-port-edit-chassis.html
- Static ARP: interfaces/interface/unit/family/inet/address/arp, адрес соседа в name,
  MAC в mac. Feature Explorer перечисляет Static ARP для EX4650-48Y с 18.3R1.
  https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/ref/statement/arp-edit-interfaces.html
  https://apps.juniper.net/feature-explorer/feature/2903
- Статический маршрут: routing-options/static/route с name и next-hop.
  В route/name находится префикс назначения (${REMOTE_PREFIX}), не условное имя маршрута;
  отдельного destination-prefix в этой структуре нет.
  Адрес next-hop задаётся текстом: <next-hop>${NEXT_HOP_IP}</next-hop>, без вложенного address или name.
  https://www.juniper.net/documentation/us/en/software/junos/junos-xml-protocol/topics/task/junos-xml-protocol-requesting-configuration-information-identifier-indicator.html
  https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/ref/statement/static-edit-routing-options.html

Version, Reserved0, Protocol Type, checksum и предотвращение петель проверяются тестом;
не изобретай переключатели этих полей. Поддержку конкретного поведения нужно подтвердить.
