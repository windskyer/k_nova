
drop table if exists adapter_ethernet_virtual;

drop table if exists host_network_association;

drop table if exists adapter_ethernet_shared;

drop table if exists port_fc;

drop table if exists server_vio2;

drop table if exists scg_vios_association;

drop table if exists storage_connectivity_group;

drop table if exists server_vio;


drop table if exists compute_node_health_status;

drop table if exists ibm_hmcs;


drop table if exists ibm_hmc_health_status;

drop table if exists ibm_hmc_hosts;

drop table if exists ibm_hmc_host_clusters;

drop table if exists instance_health_status;

drop table if exists instance_power_specs;

drop table if exists onboard_tasks;

drop table if exists onboard_task_servers;

/*==============================================================*/
/* table: adapter_ethernet_shared                               */
/*==============================================================*/
create table adapter_ethernet_shared
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int  not null auto_increment,
   adapter_name         varchar(255) not null ,
   slot                 int,
   state                varchar(255),
   primary_vea_pk_id    int not null,
   control_channel_vea_pk_id int,
   vios_pk_id           int not null,
   primary key(pk_id)
   
);

/*==============================================================*/
/* table: adapter_ethernet_virtual                              */
/*==============================================================*/
create table adapter_ethernet_virtual
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int    not null  auto_increment,
   adapter_name         varchar(255) not null,
   slot                 int,
   state                varchar(255),
   pvid                 int,
   is_trunk             smallint,
   trunk_priority       int,
   is_ieee_ethernet     smallint,
   vswitch_name         varchar(255),
   vlan_ids_string      varchar(255),
   sea_pk_id            int,
   vios_pk_id           int not null,
   primary key(pk_id)
);

/*==============================================================*/
/* table: compute_node_health_status                            */
/*==============================================================*/
create table compute_node_health_status
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   varchar(255)  not null,
   health_state         varchar(255),
   reason               text,
   unknown_reason_details text,
   primary key(id)
);
/*==============================================================*/
/* table: host_network_association                              */
/*==============================================================*/
create table host_network_association
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int  not null auto_increment ,
   neutron_network_id   varchar(255) not null,
   sea_pk_id            int,
   compute_node_id      int not null,
   primary key(pk_id)
);

/*==============================================================*/
/* table: ibm_hmcs                                              */
/*==============================================================*/
create table ibm_hmcs
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   int not null  auto_increment,
   uuid                 varchar(255) not null,
   display_name         varchar(255) not null,
   access_ip            varchar(255) not null,
   user_id              varchar(255) not null,
   user_credentials     varchar(255) not null,
   registered_at        timestamp,
   primary key(id)
);

/*==============================================================*/
/* table: ibm_hmc_health_status                                 */
/*==============================================================*/
create table ibm_hmc_health_status
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   varchar(255)  not null,
   health_state         varchar(255),
   reason               text,
   unknown_reason_details text,
   primary key(id)
);

/*==============================================================*/
/* table: ibm_hmc_hosts                                         */
/*==============================================================*/
create table ibm_hmc_hosts
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   int  not null auto_increment,
   hmc_uuid             varchar(255) not null,
   host_name            varchar(255) not null,
   primary key(id)
);

/*==============================================================*/
/* table: ibm_hmc_host_clusters                                 */
/*==============================================================*/
create table ibm_hmc_host_clusters
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   int  not null  auto_increment,
   hmc_uuid             varchar(255) not null,
   host_name            varchar(255) not null,
   cluster_id           varchar(255) not null,
   primary key(id)
);

/*==============================================================*/
/* table: instance_health_status                                */
/*==============================================================*/
create table instance_health_status
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   varchar(255)  not null,
   health_state         varchar(255),
   reason               text,
   unknown_reason_details text,
   primary key(id)
);

/*==============================================================*/
/* table: instance_power_specs                                  */
/*==============================================================*/
create table instance_power_specs
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   int  not null auto_increment,
   instance_uuid        varchar(255) not null,
   vm_id                varchar(255),
   vm_uuid              varchar(255),
   vm_name              varchar(255),
   vios_ids             varchar(255),
   vcpu_mode            varchar(255),
   memory_mode          varchar(255),
   vcpus                int,
   min_vcpus            int,
   max_vcpus            int,
   proc_units           float,
   min_proc_units       float,
   max_proc_units       float,
   memory_mb            int,
   min_memory_mb        int,
   max_memory_mb        int,
   root_gb              int,
   ephemeral_gb         int,
   pending_action       varchar(255),
   pending_onboard_actions varchar(255),
   processor_compatibility varchar(255),
   current_compatibility_mode varchar(255),
   desired_compatibility_mode varchar(255),
   dedicated_sharing_mode varchar(255),
   dedicated_proc       varchar(255),
   avail_priority       int,
   shared_weight        int,
   rmc_state            varchar(255),
   uncapped             smallint,
   operating_system     varchar(255),
   cpu_topology         text,
   primary key(id)
);
/*==============================================================*/
/* table: onboard_tasks                                         */
/*==============================================================*/
create table onboard_tasks
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   int  not null auto_increment,
   host                 varchar(255) not null,
   status               varchar(255) not null,
   started_at           timestamp not null,
   ended_at             timestamp,
   progress             int not null,
   primary key(id)
);
/*==============================================================*/
/* table: onboard_task_servers                                  */
/*==============================================================*/
create table onboard_task_servers
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   int  not null  auto_increment,
   task_id              int not null,
   server_uuid          varchar(255) not null,
   server_name          varchar(255) not null,
   status               varchar(255) not null,
   fault_message        varchar (255),
   primary key (id)
);

/*==============================================================*/
/* table: port_fc                                               */
/*==============================================================*/
create table port_fc
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   id                   varchar(255) not null,
   name                 varchar(255),
   status               varchar(255),
   enabled              smallint,
   wwpn                 varchar(255),
   adapter_id           varchar(255) not null,
   port_tag             varchar(255),
   fabric               varchar(255),
   pk_id                int  not null auto_increment,
   vios_pk_id           int not null,
   primary key(pk_id)
);

/*==============================================================*/
/* table: scg_vios_association                                  */
/*==============================================================*/
create table scg_vios_association
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int  not null auto_increment,
   scg_pk_id            int not null,
   vios_pk_id           int not null,
   primary key(pk_id)
);

/*==============================================================*/
/* table: server_vio                                            */
/*==============================================================*/
create table server_vio
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int  not null  auto_increment,
   lpar_id              int not null,
   lpar_name            varchar(255) not null,
   state                varchar(255),
   rmc_state            varchar(255),
   cluster_provider_name varchar(255),
   compute_node_id      int not null,
   primary key(pk_id)
);

/*==============================================================*/
/* table: server_vio2                                           */
/*==============================================================*/
create table server_vio2
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int  not null auto_increment,
   lpar_id              int not null,
   lpar_name            varchar(255) not null,
   state                varchar(255),
   rmc_state            varchar(255),
   cluster_provider_name varchar(255),
   compute_node_id      int not null,
   primary key(pk_id)
);

/*==============================================================*/
/* table: storage_connectivity_group                            */
/*==============================================================*/
create table storage_connectivity_group
(
   created_at           timestamp,
   updated_at           timestamp,
   deleted_at           timestamp,
   deleted              int,
   pk_id                int  not null auto_increment,
   id                   varchar(255) not null,
   display_name         varchar(255) not null,
   enabled              smallint,
   auto_defined         smallint,
   auto_add_vios        smallint,
   fc_storage_access    smallint,
   port_tag             varchar(255),
   cluster_provider_name varchar(255),
   priority_cluster     int,
   priority_external    int,
   primary key(pk_id)
);

alter table adapter_ethernet_shared add constraint fk_server_enth_sha foreign key (vios_pk_id)
      references server_vio (pk_id) on delete restrict on update restrict;

alter table adapter_ethernet_virtual add constraint fk_enthrent_sea foreign key (sea_pk_id)
      references adapter_ethernet_shared (pk_id);

alter table adapter_ethernet_virtual add constraint fk_server_enth_vir foreign key (vios_pk_id)
      references server_vio (pk_id) on delete restrict on update restrict;

alter table host_network_association add constraint fk_compute_network foreign key (compute_node_id)
      references compute_nodes (id) on delete restrict on update restrict;

alter table host_network_association add constraint fk_enther_netsea foreign key (sea_pk_id)
      references adapter_ethernet_shared (pk_id);

alter table port_fc add constraint fk_server_port foreign key (vios_pk_id)
      references server_vio (pk_id) on delete restrict on update restrict;

alter table scg_vios_association add constraint fk_scg_server foreign key (vios_pk_id)
      references server_vio (pk_id) on delete restrict on update restrict;

alter table scg_vios_association add constraint fk_scg_storage_group foreign key (scg_pk_id)
      references storage_connectivity_group (pk_id) on delete restrict on update restrict;

alter table server_vio add constraint fk_compute_scg foreign key (compute_node_id)
      references compute_nodes (id) on delete restrict on update restrict;

alter table server_vio2 add constraint fk_compute_server2 foreign key (compute_node_id)
      references compute_nodes (id) on delete restrict on update restrict;
